"""CLI for Milestone 2: single-agent tool loop + baseline measurement.

    python -m agent.run --bug BUG-001
    python -m agent.run --all
    python -m agent.run --all --repeats 3
"""

import argparse
import json
import sys
import time
from pathlib import Path

from agent import aggregate, config, sandbox, scoring
from agent.loop import LoopLimits, run_agent_loop
from agent.logging_utils import TrajectoryLogger
from agent.workspace import materialize_workspace

BUGS_DIR = config.ROOT / "bugzoo" / "bugs"
WORKSPACES_ROOT = config.ROOT / "reports" / "tmp" / "agent_workspaces"
RUNS_DIR = config.ROOT / "reports" / "runs"
REPORTS_DIR = config.ROOT / "reports"


def _run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _load_meta(bug_dir: Path) -> dict:
    return json.loads((bug_dir / "meta.json").read_text(encoding="utf-8"))


def run_one_bug(
    bug_id: str,
    run_dir: Path,
    workspaces_root: Path,
    model: str,
    repeat_index: int | None = None,
) -> dict:
    bug_dir = BUGS_DIR / bug_id
    meta = _load_meta(bug_dir)
    issue_text = (bug_dir / "issue.md").read_text(encoding="utf-8")

    workspace = workspaces_root / bug_id
    materialize_workspace(workspace, bug_dir / "files")

    log_name = f"{bug_id}.jsonl" if repeat_index is None else f"{bug_id}_rep{repeat_index}.jsonl"
    logger = TrajectoryLogger(run_dir / log_name)
    try:
        result = run_agent_loop(issue_text, workspace, model, logger, LoopLimits())
    finally:
        logger.close()

    score = scoring.score_run(workspace)

    return {
        "id": bug_id,
        "category": meta.get("category"),
        "fixed": score["fixed"],
        "stop_reason": result.stop_reason,
        "tool_calls": result.tool_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost_usd": result.estimated_cost_usd,
        "wall_time_s": round(result.wall_time_s, 2),
        "tests_tampered": score["tests_tampered"],
        "injection_flag": result.injection_flag,
    }


def print_table(rows: list[dict]) -> None:
    headers = [
        "ID", "Fixed", "Stop Reason", "Tool Calls", "In Tok", "Out Tok",
        "Est. Cost ($)", "Wall(s)", "Tampered", "Injection",
    ]
    table_rows = []
    for r in rows:
        table_rows.append(
            [
                r["id"], str(r["fixed"]), r["stop_reason"], str(r["tool_calls"]),
                str(r["input_tokens"]), str(r["output_tokens"]),
                f"{r['estimated_cost_usd']:.6f}", f"{r['wall_time_s']:.2f}",
                str(r["tests_tampered"]), str(r["injection_flag"]),
            ]
        )
    widths = [max(len(h), *(len(row[i]) for row in table_rows)) for i, h in enumerate(headers)]

    def fmt(cols: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cols, widths))

    print()
    print("All costs below are ESTIMATED from config/pricing.json token rates, not billing data.")
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in table_rows:
        print(fmt(row))


def print_repeats_table(per_bug: list[dict]) -> None:
    headers = [
        "ID", "Fixed/N", "Fix Rate", "ToolCalls(mean/min/max)", "Mean Cost ($)", "Mean Wall(s)",
    ]
    table_rows = []
    for r in per_bug:
        table_rows.append(
            [
                r["id"],
                f"{r['fixed_count']}/{r['repeats']}",
                f"{r['fix_rate']:.2f}",
                f"{r['tool_calls_mean']:.2f}/{r['tool_calls_min']}/{r['tool_calls_max']}",
                f"{r['cost_mean_usd']:.6f}",
                f"{r['wall_time_mean_s']:.2f}",
            ]
        )
    widths = [max(len(h), *(len(row[i]) for row in table_rows)) for i, h in enumerate(headers)]

    def fmt(cols: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cols, widths))

    print()
    print("All costs below are ESTIMATED from config/pricing.json token rates, not billing data.")
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in table_rows:
        print(fmt(row))


def run_bugs_with_repeats(
    bug_ids: list[str], run_dir: Path, workspaces_root: Path, model: str, repeats: int
) -> tuple[list[dict], list[dict]]:
    """Run every bug in `bug_ids` `repeats` times. Returns (per_bug_summaries, all_attempts)."""
    per_bug = []
    all_attempts: list[dict] = []
    for bug_id in bug_ids:
        attempts = []
        for rep in range(1, repeats + 1):
            print(f"Running {bug_id} (repeat {rep}/{repeats})...")
            row = run_one_bug(bug_id, run_dir, workspaces_root, model, repeat_index=rep)
            attempts.append(row)
        per_bug.append(aggregate.aggregate_bug_repeats(bug_id, attempts[0].get("category"), attempts))
        all_attempts.extend(attempts)
    return per_bug, all_attempts


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bug-fixing agent against the bug zoo.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bug", help="Run a single bug, e.g. BUG-001")
    group.add_argument("--all", action="store_true", help="Run every bug in bugzoo/bugs")
    parser.add_argument(
        "--repeats", type=int, default=1,
        help="Run each selected bug this many times and write one combined, aggregated report.",
    )
    args = parser.parse_args()

    if args.repeats < 1:
        print("--repeats must be >= 1", file=sys.stderr)
        return 1

    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set (checked .env and the environment). Aborting.", file=sys.stderr)
        return 1

    model = config.AGENT_MODEL
    run_id = _run_id()
    run_dir = RUNS_DIR / run_id
    workspaces_root = WORKSPACES_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workspaces_root.mkdir(parents=True, exist_ok=True)

    print(f"Model: {model}")
    print("Warming up sandbox container...")
    sandbox.warm_up()

    if args.bug:
        bug_ids = [args.bug]
    else:
        bug_ids = sorted(p.name for p in BUGS_DIR.iterdir() if p.is_dir() and p.name.startswith("BUG-"))

    if args.repeats == 1:
        rows = []
        for bug_id in bug_ids:
            print(f"Running {bug_id}...")
            row = run_one_bug(bug_id, run_dir, workspaces_root, model)
            rows.append(row)

        print_table(rows)

        report_path = REPORTS_DIR / f"baseline_{model}_{run_id}.json"
        report_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {report_path}")
        print(f"Trajectory logs: {run_dir}")
        return 0

    per_bug, all_attempts = run_bugs_with_repeats(bug_ids, run_dir, workspaces_root, model, args.repeats)
    overall = aggregate.aggregate_overall(per_bug, all_attempts)

    print_repeats_table(per_bug)
    print(
        f"\nOverall: {overall['overall_fix_rate']:.2%} fix rate over "
        f"{overall['total_attempts']} attempts ({overall['bugs']} bugs x {args.repeats} repeats). "
        f"Total estimated cost: ${overall['total_estimated_cost_usd']:.6f}"
    )

    report_path = REPORTS_DIR / f"repeats_{model}_{args.repeats}x_{run_id}.json"
    report_path.write_text(
        json.dumps(
            {
                "model": model,
                "run_id": run_id,
                "repeats": args.repeats,
                "per_bug": per_bug,
                "overall": overall,
                "attempts": all_attempts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {report_path}")
    print(f"Trajectory logs: {run_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
