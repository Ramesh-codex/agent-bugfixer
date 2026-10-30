"""CLI: the human approval gate for fix proposals (Milestone 4).

    python -m agent.review list
    python -m agent.review show <id>
    python -m agent.review approve <id> [--reason "..."] [--by NAME]
    python -m agent.review reject <id> --reason "..." [--by NAME]
    python -m agent.review pr <id> [--dry-run]

Proposals are created by `python -m agent.run --mode multi` when the
Reviewer approves or escalates. Decisions are final and record who decided
and when (--by defaults to `git config user.name`; with neither, the decision
is refused). The name is written into the PR body, so it is never taken from
the OS account. `pr` refuses anything that is not approved.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from agent import config, pr
from agent.proposals import ProposalError, ProposalStore


def git_user_name() -> str | None:
    """`git config user.name` as seen from this repo, or None if unset or git is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "config", "user.name"], cwd=config.ROOT, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def _status(p: dict) -> str:
    if p["status"] == "pending" and p["needs_decision"]:
        return "pending (NEEDS DECISION)"
    return p["status"]


def cmd_list(store: ProposalStore) -> None:
    rows = store.list()
    if not rows:
        print("No proposals.")
        return
    headers = ["ID", "Bug", "Run", "Stop", "Status", "Tests", "Est. Cost ($)", "PR"]
    table = [
        [str(p["id"]), p["attempt"], p["run_id"], p["stop_reason"], _status(p),
         "pass" if p["tests_fixed"] else "FAIL", f"{p['estimated_cost_usd']:.6f}", p["pr_url"] or "-"]
        for p in rows
    ]
    widths = [max(len(h), *(len(r[i]) for r in table)) for i, h in enumerate(headers)]
    for cols in [headers, ["-" * w for w in widths], *table]:
        print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))


def cmd_show(store: ProposalStore, proposal_id: int) -> None:
    p = store.get(proposal_id)
    research = p["research"] or {}
    print(f"Proposal {p['id']}: {p['attempt']}  run {p['run_id']}  ({p['model']}, reviewer {p['reviewer_version']})")
    print(f"Created:  {p['created_at']}")
    print(f"Status:   {_status(p)}")
    if p["decided_by"]:
        print(f"Decided:  {p['status']} by {p['decided_by']} at {p['decided_at']}")
        if p["decision_reason"]:
            print(f"Reason:   {p['decision_reason']}")
    print(f"Stop:     {p['stop_reason']}")
    if p["needs_decision"]:
        print("\nREVIEWER ESCALATED -- question for a human:")
        for line in (p["reviewer_question"] or "(no reasons given)").splitlines():
            print(f"  {line}")
    print(f"\nTests (pristine suite, sandbox): {'PASS' if p['tests_fixed'] else 'FAIL'}"
          + (f" -- {p['tests_summary']}" if p["tests_summary"] else ""))
    print(f"Tests modified by agent: {p['tests_tampered']}")
    print(f"Estimated cost: ${p['estimated_cost_usd']:.6f} (ESTIMATED from config/pricing.json, not billing data)")
    if p["pr_branch"] or p["pr_url"]:
        print(f"PR: branch {p['pr_branch']}  url {p['pr_url'] or '(not opened yet)'}")

    print("\nResearch summary:")
    print(f"  root cause: {research.get('root_cause', '(none)')}")
    print(f"  suspected files: {', '.join(research.get('suspected_files', [])) or '-'}")
    print(f"  confidence: {research.get('confidence', '-')}")

    print(f"\nReviewer decisions ({len(p['review_decisions'])}):")
    for i, d in enumerate(p["review_decisions"], start=1):
        print(f"  {i}. {d['decision']}")
        for reason in d["reasons"]:
            print(f"     reason: {reason}")
        for change in d.get("required_changes", []):
            print(f"     required: {change}")

    print(f"\nDiff ({', '.join(p['changed_files']) or 'no files changed'}):")
    print(p["diff"] or "(empty)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Approve or reject fix proposals; open draft PRs.")
    parser.add_argument("--db", type=Path, default=config.PROPOSALS_DB, help="Proposal database (default reports/proposals.sqlite)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List all proposals")
    for name in ("show", "approve", "reject", "pr"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
        if name in ("approve", "reject"):
            sp.add_argument("--reason", required=name == "reject")
            sp.add_argument("--by", default=None, help="Who is deciding (default: git config user.name)")
        if name == "pr":
            sp.add_argument("--dry-run", action="store_true", help="Print the plan; no git, no network")
    args = parser.parse_args(argv)

    store = ProposalStore(args.db)
    try:
        if args.cmd == "list":
            cmd_list(store)
        elif args.cmd == "show":
            cmd_show(store, args.id)
        elif args.cmd in ("approve", "reject"):
            status = "approved" if args.cmd == "approve" else "rejected"
            decided_by = args.by or git_user_name()
            if not decided_by:
                raise ProposalError("git config user.name is not set; pass --by NAME to record who decided")
            p = store.decide(args.id, status, decided_by, args.reason)
            print(f"Proposal {p['id']} {p['status']} by {p['decided_by']} at {p['decided_at']}.")
        elif args.cmd == "pr":
            settings = pr.PRSettings.from_config()
            proposal = store.get(args.id)
            pr.check_can_open_pr(proposal)
            if args.dry_run:
                print(pr.redact(pr.describe_dry_run(proposal, settings), settings.token))
            else:
                pr.open_draft_pr(store, args.id, settings)
    except (ProposalError, pr.PRError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
