"""Milestone 4: turn an APPROVED proposal into a draft PR.

    git: clean-check the local clone -> branch fix/<bug>-<run_id> off the base
         -> git apply the proposal's diff to the index -> commit -> push
    GitHub REST: look for an existing PR from that branch, else open a DRAFT one
    store the PR URL on the proposal

The approval gate is enforced here, in code (check_can_open_pr), for real
runs and dry runs alike. Idempotent: a proposal with a stored PR URL is never
re-PR'd; an existing local branch is reused instead of re-committed; an
existing PR for the branch is adopted instead of a second one being opened.

The GitHub token is only ever placed in the Authorization header of
GitHubClient's requests. Git never sees it (push authenticates through the
clone's own credential helper), and every message this module emits passes
through redact() as a backstop.

The PR body never includes the issue report (BUG-014's contains a prompt
injection): only the bug id, a one-line summary built by our code from
meta.json, and model-written text fenced as inert code blocks.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from agent import config
from agent.proposals import ProposalStore

GITHUB_API = "https://api.github.com"
PATCH_DIR = config.ROOT / "reports" / "tmp" / "pr_patches"


class PRError(Exception):
    """The PR step was refused or failed. Its message is already redacted."""


@dataclass
class PRSettings:
    repo: str | None  # "owner/name"
    token: str | None
    clone_path: Path | None
    base_branch: str = "main"
    subdir: str = ""

    @classmethod
    def from_config(cls) -> "PRSettings":
        return cls(
            repo=config.GITHUB_REPO,
            token=config.GITHUB_TOKEN,
            clone_path=Path(config.TARGET_REPO_PATH) if config.TARGET_REPO_PATH else None,
            base_branch=config.GITHUB_BASE_BRANCH,
            subdir=config.TARGET_REPO_SUBDIR.strip("/\\"),
        )

    def missing(self) -> list[str]:
        names = {"GITHUB_REPO": self.repo, "GITHUB_TOKEN": self.token, "TARGET_REPO_PATH": self.clone_path}
        return [name for name, value in names.items() if not value]


def redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


# --- gate ----------------------------------------------------------------

def check_can_open_pr(proposal: dict) -> None:
    """Raise PRError unless this proposal may become a PR."""
    pid = proposal["id"]
    if proposal["status"] != "approved":
        detail = " (escalated: needs a human decision)" if proposal["needs_decision"] else ""
        raise PRError(f"proposal {pid} is {proposal['status']}{detail}; only approved proposals become PRs")
    if not proposal["diff"].strip():
        raise PRError(f"proposal {pid} has an empty diff")
    if proposal["tests_tampered"] or any(f.replace("\\", "/").startswith("tests/") for f in proposal["changed_files"]):
        raise PRError(f"proposal {pid} changes tests/; the tests are the specification")


# --- PR content -----------------------------------------------------------

def branch_name(proposal: dict) -> str:
    rep = proposal["attempt"][len(proposal["bug_id"]):]  # "" or "_rep2"
    return f"fix/{proposal['bug_id']}{rep}-{proposal['run_id']}"


def one_line_summary(proposal: dict) -> str:
    """Written by our code from meta.json + the diff -- never from issue.md."""
    category = proposal.get("category") or "unclassified"
    files = ", ".join(proposal["changed_files"]) or "no files"
    return f"Fixes the {category} bug seeded as {proposal['bug_id']} in the bug zoo; edits {files}."


def pr_title(proposal: dict) -> str:
    return f"Fix {proposal['bug_id']} ({proposal.get('category') or 'bug'})"


def _fenced(text: str) -> str:
    """Model-written text as an inert code block: no markdown, links, or
    @mentions are rendered from it, and it cannot close its own fence."""
    longest = max((len(m) for m in re.findall(r"~+", text)), default=0)
    fence = "~" * max(3, longest + 1)
    return f"{fence}text\n{text.strip()}\n{fence}"


def build_pr_body(proposal: dict) -> str:
    research = proposal.get("research") or {}
    parts = [
        f"**{proposal['bug_id']}** -- {one_line_summary(proposal)}",
        "Draft PR opened by agent-bugfixer after human approval. Model-written text below is "
        "quoted verbatim in code blocks.",
        "### Root cause (Researcher)",
        _fenced(research.get("root_cause") or "(no research report)"),
        "### Reviewer decisions",
    ]
    for i, d in enumerate(proposal["review_decisions"], start=1):
        parts.append(f"{i}. **{d['decision']}**")
        parts.append(_fenced("\n".join(f"- {r}" for r in d["reasons"])))
    tests = "PASS" if proposal["tests_fixed"] else "FAIL"
    parts += [
        "### Test results",
        f"Full suite against the pristine tests, in the sandbox: **{tests}**"
        + (f" (`{proposal['tests_summary']}`)" if proposal["tests_summary"] else ""),
        f"Tests modified by the agent: {'yes' if proposal['tests_tampered'] else 'no'}",
        "### Estimated cost",
        f"${proposal['estimated_cost_usd']:.6f} -- ESTIMATED from config/pricing.json token rates "
        f"({proposal['model']}), not billing data.",
        "### Approval",
        f"Proposal #{proposal['id']} (run `{proposal['run_id']}`, reviewer {proposal['reviewer_version']}, "
        f"stop reason `{proposal['stop_reason']}`), approved by {proposal['decided_by']} at "
        f"{proposal['decided_at']}.",
    ]
    if proposal.get("decision_reason"):
        parts.append(_fenced(proposal["decision_reason"]))
    return "\n\n".join(parts) + "\n"


def commit_message(proposal: dict) -> str:
    return f"{pr_title(proposal)}\n\n{one_line_summary(proposal)}\n\nagent-bugfixer proposal #{proposal['id']}, run {proposal['run_id']}.\n"


# --- side effects (both injectable for tests) ------------------------------

class GitRunner:
    def __call__(self, args: list[str], cwd: Path) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise PRError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout


class GitHubClient:
    def __init__(self, token: str, session: requests.Session | None = None, api: str = GITHUB_API):
        self._token = token
        self._session = session or requests.Session()
        self._api = api

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            resp = self._session.request(method, self._api + path, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise PRError(redact(f"{method} {path} failed: {exc}", self._token)) from None
        if resp.status_code >= 400:
            raise PRError(redact(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}", self._token))
        return resp

    def find_pr(self, repo: str, branch: str) -> str | None:
        owner = repo.split("/")[0]
        resp = self._request("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all"})
        pulls = resp.json()
        return pulls[0]["html_url"] if pulls else None

    def create_draft_pr(self, repo: str, *, title: str, head: str, base: str, body: str) -> str:
        resp = self._request(
            "POST", f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        return resp.json()["html_url"]


# --- the step itself -------------------------------------------------------

@dataclass
class PRPlan:
    branch: str
    patch_path: Path
    title: str
    body: str
    commit_message: str
    target_files: list[str] = field(default_factory=list)


def make_plan(proposal: dict, settings: PRSettings) -> PRPlan:
    prefix = f"{settings.subdir}/" if settings.subdir else ""
    return PRPlan(
        branch=branch_name(proposal),
        patch_path=PATCH_DIR / f"proposal_{proposal['id']}.patch",
        title=pr_title(proposal),
        body=build_pr_body(proposal),
        commit_message=commit_message(proposal),
        target_files=[prefix + f.replace("\\", "/") for f in proposal["changed_files"]],
    )


def _apply_args(plan: PRPlan, settings: PRSettings, check: bool) -> list[str]:
    # --cached: patch the index, not the working tree. The diff is LF; a
    # clone with core.autocrlf has CRLF working files but LF blobs.
    args = ["apply", "--cached"]
    if check:
        args.append("--check")
    if settings.subdir:
        args.append(f"--directory={settings.subdir}")
    return [*args, str(plan.patch_path)]


def describe_dry_run(proposal: dict, settings: PRSettings) -> str:
    """Exactly what open_draft_pr would do -- without running git, writing
    files, or touching the network."""
    plan = make_plan(proposal, settings)
    clone = settings.clone_path or "<TARGET_REPO_PATH not set>"
    repo = settings.repo or "<GITHUB_REPO not set>"
    lines = [f"DRY RUN for proposal {proposal['id']} -- nothing below is executed."]
    missing = settings.missing()
    if missing:
        lines.append(f"A real run would refuse: not set in .env/environment: {', '.join(missing)}")
    if proposal.get("pr_url"):
        lines.append(f"Already has a PR: {proposal['pr_url']} -- a real run would do nothing.")
        return "\n".join(lines)
    git = lambda args: f"  git -C {clone} {' '.join(args)}"  # noqa: E731
    lines += [
        "1. Refuse unless the clone's working tree is clean:",
        git(["status", "--porcelain"]),
        "2. Remember the current branch, to return to it afterwards:",
        git(["rev-parse", "--abbrev-ref", "HEAD"]),
        f"3. If branch {plan.branch} already exists locally (and is ahead of {settings.base_branch}), "
        "reuse it and skip to 8. Else:",
        git(["branch", "--list", plan.branch]),
        "4. Write the proposal diff to a patch file and create the branch:",
        f"  write {len(proposal['diff'])} chars -> {plan.patch_path}",
        git(["checkout", "-b", plan.branch, settings.base_branch]),
        "5. Apply the diff to the index (on failure: undo, delete the branch, stop):",
        git(_apply_args(plan, settings, check=True)),
        git(_apply_args(plan, settings, check=False)),
        "6. Commit:",
        git(["commit", "-m", "<message below>"]),
        "7. Sync the working tree with the commit:",
        git(["checkout", "HEAD", "--", *plan.target_files]),
        "8. Return to the original branch, then push (network; the clone's credential helper):",
        git(["checkout", "<original branch>"]),
        git(["push", "-u", "origin", plan.branch]),
        "9. Look for an existing PR from the branch (network):",
        f"  GET {GITHUB_API}/repos/{repo}/pulls?head={repo.split('/')[0]}:{plan.branch}&state=all",
        "10. If none, open a DRAFT PR (network):",
        f"  POST {GITHUB_API}/repos/{repo}/pulls  draft=true  head={plan.branch}  base={settings.base_branch}",
        "  Authorization: Bearer <GITHUB_TOKEN, redacted>",
        f"11. Store the PR URL on proposal {proposal['id']}.",
        "",
        f"Commit message:\n{plan.commit_message}",
        f"PR title: {plan.title}",
        f"PR body:\n{plan.body}",
    ]
    return "\n".join(lines)


def open_draft_pr(
    store: ProposalStore,
    proposal_id: int,
    settings: PRSettings,
    *,
    git: Callable[[list[str], Path], str] | None = None,
    github: GitHubClient | None = None,
    echo: Callable[[str], None] = print,
) -> str:
    """Open (or find) the draft PR for an approved proposal; returns its URL."""
    proposal = store.get(proposal_id)
    check_can_open_pr(proposal)
    if proposal.get("pr_url"):
        echo(f"Proposal {proposal_id} already has a PR: {proposal['pr_url']}")
        return proposal["pr_url"]
    missing = settings.missing()
    if missing:
        raise PRError(f"not set in .env/environment: {', '.join(missing)}")
    git = git or GitRunner()
    github = github or GitHubClient(settings.token)
    say = lambda msg: echo(redact(msg, settings.token))  # noqa: E731
    clone = settings.clone_path
    plan = make_plan(proposal, settings)
    try:
        if git(["status", "--porcelain"], clone).strip():
            raise PRError(f"{clone} has uncommitted changes; refusing to touch it")
        original = git(["rev-parse", "--abbrev-ref", "HEAD"], clone).strip()

        if git(["branch", "--list", plan.branch], clone).strip():
            ahead = git(["rev-list", "--count", f"{settings.base_branch}..{plan.branch}"], clone).strip()
            if ahead in ("", "0"):
                raise PRError(f"branch {plan.branch} exists but has no commits beyond {settings.base_branch}")
            say(f"Branch {plan.branch} already exists with {ahead} commit(s); reusing it.")
        else:
            plan.patch_path.parent.mkdir(parents=True, exist_ok=True)
            plan.patch_path.write_text(proposal["diff"], encoding="utf-8", newline="")
            git(["checkout", "-b", plan.branch, settings.base_branch], clone)
            try:
                git(_apply_args(plan, settings, check=True), clone)
                git(_apply_args(plan, settings, check=False), clone)
                git(["commit", "-m", plan.commit_message], clone)
                git(["checkout", "HEAD", "--", *plan.target_files], clone)
            except PRError:
                # Nothing committed: drop the half-made branch so a retry starts clean.
                git(["reset", "--quiet"], clone)
                git(["checkout", original], clone)
                git(["branch", "-D", plan.branch], clone)
                raise
            say(f"Committed the diff on {plan.branch}.")
        git(["checkout", original], clone)
        store.set_pr(proposal_id, plan.branch, None)

        git(["push", "-u", "origin", plan.branch], clone)
        say(f"Pushed {plan.branch}.")
        url = github.find_pr(settings.repo, plan.branch)
        if url:
            say(f"Found an existing PR for {plan.branch}: {url}")
        else:
            url = github.create_draft_pr(
                settings.repo, title=plan.title, head=plan.branch, base=settings.base_branch, body=plan.body
            )
            say(f"Opened draft PR: {url}")
        store.set_pr(proposal_id, plan.branch, url)
        return url
    except PRError as exc:
        raise PRError(redact(str(exc), settings.token)) from None
