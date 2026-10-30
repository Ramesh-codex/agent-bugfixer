"""CLI: run the bug-fixing agent against the bug zoo and score it.

    python -m agent.run --bug BUG-001                 # single agent (M2 loop)
    python -m agent.run --mode multi --bug BUG-001    # Researcher -> Coder -> Reviewer (M3)
    python -m agent.run --mode multi --reviewer v1 --bug BUG-001   # M3 baseline Reviewer
    python -m agent.run --all --repeats 3
"""

import argparse
import json
import sys
import time
from pathlib import Path

from agent import aggregate, config, sandbox, scoring
from agent.graph import run_multi_agent
from agent.loop import LoopLimits, run_agent_loop
from agent.logging_utils import TrajectoryLogger
from agent.roles import DEFAULT_REVIEWER, REVIEWERS, AnthropicModelClient
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
    mode: str = "single",
    reviewer: str = DEFAULT_REVIEWER,
) -> dict:
    bug_dir = BUGS_DIR / bug_id
    meta = _load_meta(bug_dir)
    issue_text = (bug_dir / "issue.md").read_text(encoding="utf-8")

    workspace = workspaces_root / bug_id
    materialize_workspace(workspace, bug_dir / "files")

    stem = bug_id if repeat_index is None else f"{bug_id}_rep{repeat_index}"
    if mode == "multi":
        return _run_one_bug_multi(
            bug_id, meta, issue_text, workspace, run_dir, workspaces_root, model, stem, reviewer
        )

    logger = TrajectoryLogger(run_dir / f"{stem}.jsonl")
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


def _run_one_bug_multi(
    bug_id: str, meta: dict, issue_text: str, workspace: Path, run_dir: Path,
    workspaces_root: Path, model: str, stem: str, reviewer: str,
) -> dict:
    # Pristine snapshot of the starting workspace (base + broken files) for
    # the Reviewer's get_diff -- outside the workspace, so no agent can edit it.
    baseline = workspaces_root / "_baseline" / stem
    materialize_workspace(baseline, BUGS_DIR / bug_id / "files")

    thread_id = f"{run_dir.name}/{stem}"
    logger = TrajectoryLogger(run_dir / f"{stem}_multi_{reviewer}.jsonl")
    try:
        result = run_multi_agent(
            issue_text, workspace, baseline, AnthropicModelClient(model), logger,
            checkpoint_path=run_dir / "checkpoints.sqlite", thread_id=thread_id,
            reviewer_version=reviewer,
        )
    finally:
        logger.close()

    # Same scoring as single mode, only after the graph has ended.
    score = scoring.score_run(workspace)

    return {
        "id": bug_id,
        "mode": "multi",
        "reviewer_version": result.reviewer_version,
        "category": meta.get("category"),
        "fixed": score["fixed"],
        "stop_reason": result.stop_reason,
        "stop_agent": result.stop_agent,
        "revisions": result.revisions,
        "reviewer_decision": result.reviewer_decision,
        "review_decisions": result.review_decisions,
        "tool_calls": result.tool_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost_usd": result.estimated_cost_usd,
        "cost_by_agent_usd": {a: u["estimated_cost_usd"] for a, u in result.usage.items()},
        "per_agent": result.usage,
        "wall_time_s": round(result.wall_time_s, 2),
        "tests_tampered": score["tests_tampered"],
        "injection_flag": result.injection_flag,
        "thread_id": thread_id,
    }


def _cost_split(costs: dict) -> str:
    return f"{costs['researcher']:.6f}/{costs['coder']:.6f}/{costs['reviewer']:.6f}"


def print_table(rows: list[dict]) -> None:
    multi = any(r.get("mode") == "multi" for r in rows)
    headers = [
        "ID", "Fixed", "Stop Reason", "Tool Calls", "In Tok", "Out Tok",
        "Est. Cost ($)", "Wall(s)", "Tampered", "Injection",
    ]
    if multi:
        headers += ["Revisions", "Reviewer", "Cost R/C/Rv ($)"]
    table_rows = []
    for r in rows:
        cols = [
            r["id"], str(r["fixed"]), r["stop_reason"], str(r["tool_calls"]),
            str(r["input_tokens"]), str(r["output_tokens"]),
            f"{r['estimated_cost_usd']:.6f}", f"{r['wall_time_s']:.2f}",
            str(r["tests_tampered"]), str(r["injection_flag"]),
        ]
        if multi:
            cols += [str(r["revisions"]), str(r["reviewer_decision"]), _cost_split(r["cost_by_agent_usd"])]
        table_rows.append(cols)
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
    multi = any("revisions_mean" in r for r in per_bug)
    headers = [
        "ID", "Fixed/N", "Fix Rate", "Tampered/N", "Injection/N",
        "ToolCalls(mean/min/max)", "Mean Cost ($)", "Mean Wall(s)", "Stop Reasons",
    ]
    if multi:
        headers += ["Mean Revisions", "Reviewer (final)", "Mean Cost R/C/Rv ($)"]
    table_rows = []
    for r in per_bug:
        cols = [
                r["id"],
                f"{r['fixed_count']}/{r['repeats']}",
                f"{r['fix_rate']:.2f}",
                f"{r['tampered_count']}/{r['repeats']}",
                f"{r['injection_count']}/{r['repeats']}",
                f"{r['tool_calls_mean']:.2f}/{r['tool_calls_min']}/{r['tool_calls_max']}",
                f"{r['cost_mean_usd']:.6f}",
                f"{r['wall_time_mean_s']:.2f}",
                aggregate.format_counts(r["stop_reasons"]),
            ]
        if multi:
            decisions = ", ".join(f"{k}:{v}" for k, v in sorted(r["reviewer_decisions"].items()))
            cols += [f"{r['revisions_mean']:.2f}", decisions, _cost_split(r["cost_by_agent_mean_usd"])]
        table_rows.append(cols)
    widths = [max(len(h), *(len(row[i]) for row in table_rows)) for i, h in enumerate(headers)]

    def fmt(cols: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cols, widths))

    print()
    print("All costs below are ESTIMATED from config/pricing.json token rates, not billing data.")
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in table_rows:
        print(fmt(row))


def print_overall(overall: dict, repeats: int) -> None:
    print(
        f"\nOverall: {overall['overall_fix_rate']:.2%} fix rate over "
        f"{overall['total_attempts']} attempts ({overall['bugs']} bugs x {repeats} repeats). "
        f"Total estimated cost: ${overall['total_estimated_cost_usd']:.6f}"
    )
    print(f"Stop reasons: {aggregate.format_counts(overall['stop_reasons'])}")


def print_review_decisions(rows: list[dict]) -> None:
    for r in rows:
        print(f"\n{r['id']} reviewer decisions ({len(r['review_decisions'])}):")
        for i, d in enumerate(r["review_decisions"], start=1):
            print(f"  {i}. {d['decision']}")
            for reason in d["reasons"]:
                print(f"     reason: {reason}")
            for change in d["required_changes"]:
                print(f"     required: {change}")


def report_filename(kind: str, mode: str, reviewer: str | None, model: str, run_id: str,
                    repeats: int | None = None) -> str:
    """e.g. baseline_single_<model>_<id>.json, repeats_multi_v2_<model>_3x_<id>.json."""
    tag = mode if mode == "single" else f"{mode}_{reviewer}"
    count = f"_{repeats}x" if repeats is not None else ""
    return f"{kind}_{tag}_{model}{count}_{run_id}.json"


def run_bugs_with_repeats(
    bug_ids: list[str], run_dir: Path, workspaces_root: Path, model: str, repeats: int,
    mode: str = "single",
    reviewer: str = DEFAULT_REVIEWER,
) -> tuple[list[dict], list[dict]]:
    """Run every bug in `bug_ids` `repeats` times. Returns (per_bug_summaries, all_attempts)."""
    per_bug = []
    all_attempts: list[dict] = []
    for bug_id in bug_ids:
        attempts = []
        for rep in range(1, repeats + 1):
            print(f"Running {bug_id} (repeat {rep}/{repeats})...")
            row = run_one_bug(bug_id, run_dir, workspaces_root, model, repeat_index=rep, mode=mode,
                              reviewer=reviewer)
            attempts.append(row)
        summary = aggregate.aggregate_bug_repeats(bug_id, attempts[0].get("category"), attempts)
        if mode == "multi":
            summary.update(aggregate.aggregate_multi_extras(attempts))
        per_bug.append(summary)
        all_attempts.extend(attempts)
    return per_bug, all_attempts


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bug-fixing agent against the bug zoo.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bug", help="Run a single bug, e.g. BUG-001")
    group.add_argument("--all", action="store_true", help="Run every bug in bugzoo/bugs")
    parser.add_argument(
        "--mode", choices=["single", "multi"], default="single",
        help="single: the M2 tool loop. multi: Researcher -> Coder -> Reviewer LangGraph.",
    )
    parser.add_argument(
        "--reviewer", choices=sorted(REVIEWERS), default=None,
        help=f"Reviewer version for --mode multi (default {DEFAULT_REVIEWER}). v1 = M3 baseline.",
    )
    parser.add_argument(
        "--repeats", type=int, default=1,
        help="Run each selected bug this many times and write one combined, aggregated report.",
    )
    args = parser.parse_args()

    if args.repeats < 1:
        print("--repeats must be >= 1", file=sys.stderr)
        return 1

    if args.reviewer and args.mode != "multi":
        print("--reviewer only applies to --mode multi", file=sys.stderr)
        return 1
    reviewer = (args.reviewer or DEFAULT_REVIEWER) if args.mode == "multi" else None

    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set (checked .env and the environment). Aborting.", file=sys.stderr)
        return 1

    model = config.AGENT_MODEL
    run_id = _run_id()
    run_dir = RUNS_DIR / run_id
    workspaces_root = WORKSPACES_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workspaces_root.mkdir(parents=True, exist_ok=True)

    print(f"Model: {model}  Mode: {args.mode}" + (f"  Reviewer: {reviewer}" if reviewer else ""))
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
            row = run_one_bug(bug_id, run_dir, workspaces_root, model, mode=args.mode, reviewer=reviewer)
            rows.append(row)

        print_table(rows)
        if args.mode == "multi":
            print_review_decisions(rows)
            print(f"\nTotal estimated cost: ${sum(r['estimated_cost_usd'] for r in rows):.6f}")

        report_path = REPORTS_DIR / report_filename("baseline", args.mode, reviewer, model, run_id)
        report_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {report_path}")
        print(f"Trajectory logs: {run_dir}")
        return 0

    per_bug, all_attempts = run_bugs_with_repeats(
        bug_ids, run_dir, workspaces_root, model, args.repeats, mode=args.mode, reviewer=reviewer
    )
    overall = aggregate.aggregate_overall(per_bug, all_attempts)

    print_repeats_table(per_bug)
    if args.mode == "multi":
        print_review_decisions(all_attempts)
    print_overall(overall, args.repeats)

    report_path = REPORTS_DIR / report_filename("repeats", args.mode, reviewer, model, run_id, args.repeats)
    report_path.write_text(
        json.dumps(
            {
                "mode": args.mode,
                "reviewer": reviewer,
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
