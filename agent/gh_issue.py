"""Milestone 6 (live mode): run the multi-agent v2 graph on ONE GitHub issue
against any Python repo whose tests run with pytest.

    python -m agent.gh_issue --repo-path <dir> --issue-file <json> [--out-dir <dir>] [--run-id <id>]

The issue file is {"number": N, "title": "...", "body": "..."}; title and body
are untrusted data and only ever reach the model inside the
<untrusted_issue_report> block (agent/roles.py), never a shell or a PR.

The agents work on a copy of --repo-path (.git, .github, caches and .env* are
not copied); a second copy, taken before any agent runs, is the snapshot the
Reviewer's get_diff and the final patch are taken against. Unlike the bug zoo
there is no independently pristine tests/ -- the snapshot comes from the same
repo -- so the Coder's tests/ edit block (Reviewer v2) is the main guard, and
the final check is: the diff must touch nothing protected
(agent.gh_live.check_patch), and the full suite must pass in the sandbox
(--network none) with the snapshot's tests/ restored.

Outputs, read by .github/workflows/agent-propose.yml in the target repo (this
module makes no GitHub API calls):

    comment.md      the ONE issue comment; ends with the hidden run marker
    fix.patch       only if the diff is non-empty and passes check_patch
    labels.txt      needs-approval | needs-decision
    summary.json    machine-readable run summary
    trajectory.jsonl, checkpoints.sqlite

Hard caps come from the environment (agent/config.py): AGENT_MULTI_COST_BUDGET_USD,
AGENT_MULTI_TOKEN_BUDGET, AGENT_WALL_CLOCK_TIMEOUT_S, plus the per-role tool-call
and revision limits.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent import config, gh_live, sandbox
from agent.graph import MultiLimits, MultiResult, run_multi_agent
from agent.logging_utils import TrajectoryLogger
from agent.proposals import pytest_summary
from agent.roles import DEFAULT_REVIEWER, AnthropicModelClient, ModelClient
from agent.tools import workspace_diff

OUT_ROOT = config.ROOT / "reports" / "tmp" / "gh_issue"

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".github", "__pycache__", "*.pyc", ".pytest_cache", ".venv", "venv", ".tox",
    "node_modules", ".env", ".env.*",
)

# GitHub rejects comments over 65536 characters; the diff is cut well before
# that (the full patch is always in the workflow artifact).
MAX_COMMENT_DIFF_CHARS = 30_000
MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 20_000


class IssueFileError(Exception):
    pass


def load_issue(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IssueFileError(f"cannot read issue file: {exc}") from None
    number = data.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise IssueFileError("issue file needs a positive integer 'number'")
    title, body = data.get("title") or "", data.get("body") or ""
    if not isinstance(title, str) or not isinstance(body, str):
        raise IssueFileError("'title' and 'body' must be strings")
    return {"number": number, "title": title[:MAX_TITLE_CHARS], "body": body[:MAX_BODY_CHARS]}


def issue_text(issue: dict) -> str:
    return f"# {issue['title']}\n\n{issue['body']}".strip() + "\n"


def git_head(repo_path: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def prepare_copies(repo_path: Path, work_dir: Path) -> tuple[Path, Path]:
    """(workspace, snapshot): two identical copies of the repo. symlinks=True
    copies links as links, so one pointing outside the repo stays a link that
    resolve_in_workspace refuses, instead of being copied as file content."""
    if work_dir.resolve().is_relative_to(repo_path.resolve()):
        raise ValueError("the output directory must be outside --repo-path (it would copy itself)")
    workspace, snapshot = work_dir / "repo", work_dir / "snapshot"
    for dest in (workspace, snapshot):
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(repo_path, dest, ignore=COPY_IGNORE, symlinks=True)
    return workspace, snapshot


def final_suite(workspace: Path, snapshot: Path) -> tuple[bool, str]:
    """Full suite in the sandbox, with tests/ put back exactly as the snapshot
    had it -- same as agent/scoring.py does with the zoo's pristine tests."""
    if (workspace / "tests").exists():
        shutil.rmtree(workspace / "tests")
    if (snapshot / "tests").exists():
        shutil.copytree(snapshot / "tests", workspace / "tests", symlinks=True)
    returncode, output = sandbox.run_pytest(workspace)
    return returncode == 0, output


# --- comment ---------------------------------------------------------------

def fenced(text: str, lang: str = "text") -> str:
    """Model-written text as an inert code block (same rule as agent/pr.py):
    no markdown, links or @mentions render from it, and it cannot close its
    own fence."""
    longest = max((len(m) for m in re.findall(r"~+", text)), default=0)
    fence = "~" * max(3, longest + 1)
    return f"{fence}{lang}\n{text.strip()}\n{fence}"


_STOP_EXPLANATIONS = {
    "approved": "The Reviewer approved the fix.",
    "escalated": "The Reviewer escalated: the tests and the issue appear to conflict, so a human must decide.",
    "rejected": "The Reviewer rejected the change.",
    "max_revisions": "The Reviewer still requested changes after the maximum number of revision rounds.",
    "cost_budget": "The run hit its cost budget before finishing.",
    "token_budget": "The run hit its token budget before finishing.",
    "wall_clock_timeout": "The run hit its wall-clock limit before finishing.",
    "max_tool_calls": "An agent used up its tool-call limit before finishing.",
    "invalid_handoff": "An agent failed to produce a valid handoff twice.",
    "api_error": "The model API returned an error.",
    "refusal": "The model refused to continue.",
    "internal_error": "agent-bugfixer itself failed before finishing.",
}


def render_comment(s: dict, research: dict | None, review_decisions: list[dict], diff: str) -> str:
    n = s["issue"]
    verdict = s["verdict"]
    headline = ("Proposed fix ready for your approval" if verdict == "needs-approval"
                else "A human decision is needed")
    parts = [
        f"### agent-bugfixer: {headline}",
        f"{_STOP_EXPLANATIONS.get(s['stop_reason'], 'The run stopped.')} "
        f"Stop reason `{s['stop_reason']}`, Reviewer {s['reviewer_version']}, "
        f"{s['revisions']} revision round(s). Label: `{verdict}`.",
        "Text written by the model is quoted verbatim in code blocks.",
    ]
    if s["injection_flag"]:
        parts.append("**Warning:** the injection detector fired during this run (`.env` access or "
                     "secret-shaped output). Check the trajectory artifact before approving.")

    parts.append("#### Root cause (Researcher)")
    if research:
        parts.append(fenced(research["root_cause"]
                            + "\n\nSuspected files: " + ", ".join(research["suspected_files"])
                            + f"\nConfidence: {research['confidence']}"))
    else:
        parts.append("_No research report (the run stopped before the Researcher finished)._")

    parts.append("#### Proposed diff")
    if diff.strip():
        shown = diff if len(diff) <= MAX_COMMENT_DIFF_CHARS else diff[:MAX_COMMENT_DIFF_CHARS]
        parts.append(fenced(shown, "diff"))
        if len(shown) < len(diff):
            parts.append(f"_Diff truncated at {MAX_COMMENT_DIFF_CHARS} characters; the full patch is in "
                         "the workflow artifact._")
    else:
        parts.append("_No changes._")

    parts.append("#### Reviewer")
    if review_decisions:
        for i, d in enumerate(review_decisions, start=1):
            parts.append(f"{i}. **{d['decision']}**")
            body = "\n".join(f"- {r}" for r in d["reasons"])
            if d.get("required_changes"):
                body += "\nRequired changes:\n" + "\n".join(f"- {c}" for c in d["required_changes"])
            parts.append(fenced(body))
    else:
        parts.append("_No review (the run stopped before the Reviewer finished)._")

    parts.append("#### Test result")
    suite = "PASS" if s["tests_passed"] else "FAIL"
    summary = f" (`{s['tests_summary']}`)" if s["tests_summary"] else ""
    parts.append(f"Full test suite in the sandbox (no network), with `tests/` as it was before the run: "
                 f"**{suite}**{summary}")
    if s["patch_problems"]:
        parts.append("Protected-path check: **refused**, so no patch was saved:")
        parts.append(fenced("\n".join(f"- {p}" for p in s["patch_problems"])))
    else:
        parts.append("Protected-path check (tests, test config, CI): no changes.")

    parts.append("#### Estimated cost")
    parts.append(
        f"${s['estimated_cost_usd']:.6f} -- ESTIMATED from token counts and config/pricing.json "
        f"({s['model']}), not billing data. {s['input_tokens']} input / {s['output_tokens']} output "
        f"tokens, {s['tool_calls']} tool calls, {s['wall_time_s']:.0f} s. "
        f"Cap: ${s['cost_budget_usd']:.2f} per run."
    )

    if s["stop_reason"] == "escalated":
        last = review_decisions[-1]
        parts.append("#### Question for you")
        parts.append("The Reviewer could not approve without a human decision. Its reasons:")
        parts.append(fenced("\n".join(f"- {r}" for r in last["reasons"])))

    parts.append("#### Next step")
    if s["patch_sha256"]:
        parts.append(
            f"Comment `/approve` to push this exact patch to branch `agent/issue-{n}` and open a "
            f"**draft** PR (Refs #{n}), or `/reject` to close this out. Nothing has been pushed."
        )
    else:
        parts.append("There is no patch to approve. Comment `/reject` to close this out, or fix it by hand.")

    marker = gh_live.make_marker(run_id=s["run_id"], issue=n, patch_sha256=s["patch_sha256"],
                                 base_sha=s["base_sha"], verdict=verdict)
    return "\n\n".join(parts) + "\n\n" + marker + "\n"


# --- the run ----------------------------------------------------------------

def run_issue(
    issue: dict, repo_path: Path, out_dir: Path, run_id: str, client: ModelClient,
    limits: MultiLimits | None = None,
) -> dict:
    """Run the graph on `issue` and write every output file. Returns the
    summary dict (also written to summary.json)."""
    limits = limits or MultiLimits()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("comment.md", "fix.patch", "labels.txt", "summary.json"):
        (out_dir / name).unlink(missing_ok=True)

    base_sha = git_head(repo_path)
    workspace, snapshot = prepare_copies(repo_path, out_dir / "work")

    logger = TrajectoryLogger(out_dir / "trajectory.jsonl")
    logger.log({"event": "gh_issue_start", "run_id": run_id, "issue": issue["number"], "base_sha": base_sha})
    result: MultiResult | None = None
    try:
        result = run_multi_agent(
            issue_text(issue), workspace, snapshot, client, logger,
            checkpoint_path=out_dir / "checkpoints.sqlite", thread_id=f"issue-{issue['number']}/{run_id}",
            limits=limits, reviewer_version=DEFAULT_REVIEWER,
        )
    except Exception as exc:  # report it on the issue rather than failing silently
        logger.log({"event": "internal_error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        logger.close()

    changed_files, diff = workspace_diff(workspace, snapshot)
    patch_problems = gh_live.check_patch(diff) if diff.strip() else []
    tests_passed, tests_output = final_suite(workspace, snapshot)

    patch_sha256 = None
    if diff.strip() and not patch_problems:
        data = diff.encode("utf-8")
        (out_dir / "fix.patch").write_bytes(data)
        patch_sha256 = gh_live.sha256_hex(data)

    stop_reason = result.stop_reason if result else "internal_error"
    verdict = ("needs-approval"
               if stop_reason == "approved" and tests_passed and patch_sha256 else "needs-decision")
    summary = {
        "issue": issue["number"],
        "run_id": run_id,
        "model": client.model,
        "reviewer_version": result.reviewer_version if result else DEFAULT_REVIEWER,
        "stop_reason": stop_reason,
        "stop_agent": result.stop_agent if result else None,
        "revisions": result.revisions if result else 0,
        "verdict": verdict,
        "labels": [verdict],
        "changed_files": changed_files,
        "patch_problems": patch_problems,
        "patch_sha256": patch_sha256,
        "base_sha": base_sha,
        "tests_passed": tests_passed,
        "tests_summary": pytest_summary(tests_output),
        "injection_flag": result.injection_flag if result else False,
        "tool_calls": result.tool_calls if result else 0,
        "input_tokens": result.input_tokens if result else 0,
        "output_tokens": result.output_tokens if result else 0,
        "estimated_cost_usd": result.estimated_cost_usd if result else 0.0,
        "cost_budget_usd": limits.cost_budget_usd,
        "wall_time_s": round(result.wall_time_s, 2) if result else 0.0,
    }
    comment = render_comment(summary, result.research_report if result else None,
                             result.review_decisions if result else [], diff)
    (out_dir / "comment.md").write_text(comment, encoding="utf-8")
    (out_dir / "labels.txt").write_text(verdict + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def verify_model(model: str) -> bool:
    """CLAUDE.md rule 4: the model id must be one the API lists right now."""
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return any(m.id == model for m in client.models.list())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the multi-agent v2 pipeline on one GitHub issue.")
    parser.add_argument("--repo-path", required=True, type=Path, help="checkout of the target repo")
    parser.add_argument("--issue-file", required=True, type=Path, help='{"number", "title", "body"} JSON')
    parser.add_argument("--out-dir", type=Path, help=f"default: {config.repo_relative(OUT_ROOT)}/<run_id>")
    parser.add_argument("--run-id", default=None,
                        help="id stored in the marker; the workflow passes $GITHUB_RUN_ID (default: timestamp)")
    args = parser.parse_args(argv)

    try:
        issue = load_issue(args.issue_file)
    except IssueFileError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not args.repo_path.is_dir():
        print("--repo-path is not a directory", file=sys.stderr)
        return 1
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S")
    if not gh_live._RUN_ID_RE.match(run_id):
        print("--run-id may only contain letters, digits, '_' and '-'", file=sys.stderr)
        return 1
    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set. Aborting.", file=sys.stderr)
        return 1
    model = config.AGENT_MODEL
    if not verify_model(model):
        print(f"Model {model!r} is not in client.models.list(); refusing to run.", file=sys.stderr)
        return 1

    out_dir = args.out_dir or OUT_ROOT / run_id
    if out_dir.resolve().is_relative_to(args.repo_path.resolve()):
        print("--out-dir must be outside --repo-path", file=sys.stderr)
        return 1
    print(f"Model: {model}  Reviewer: {DEFAULT_REVIEWER}  Issue: #{issue['number']}  Run: {run_id}")
    summary = run_issue(issue, args.repo_path.resolve(), out_dir.resolve(), run_id, AnthropicModelClient(model))
    print(f"Stop reason: {summary['stop_reason']}  Verdict: {summary['verdict']}  "
          f"Tests: {'PASS' if summary['tests_passed'] else 'FAIL'}  "
          f"Estimated cost: ${summary['estimated_cost_usd']:.6f}")
    print(f"Wrote outputs to {config.repo_relative(out_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
