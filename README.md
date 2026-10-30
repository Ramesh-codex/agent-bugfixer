# agent-bugfixer

An evaluation harness for LLM coding agents. A small Python library ("the bug
zoo") has **15 seeded bugs**, two of them adversarial: one issue report carries
a prompt injection, one bug tempts the agent to edit tests instead of code. An
agent gets the issue, works in a throwaway copy of the library, and is scored
by re-running the **pristine** test suite in a locked-down Docker sandbox. Two
agent designs are compared: a single tool-using agent, and a
Researcher → Coder → Reviewer graph (LangGraph). In the multi-agent design,
nothing reaches GitHub without a human: approved or escalated fixes become
proposals, and only a human-approved proposal can become a **draft** PR.

The main result: adding a Reviewer **did not raise the fix rate**. Reviewer v2
made the outcomes **trustworthy**: 0 false "done" claims in 40 approvals, and
every failure was escalated to a human. Reviewer v1 made things worse: it
caused all 5 test-tampering events in its run.

## Architecture

```mermaid
flowchart LR
    I["Issue report<br/>(untrusted data)"] --> R["Researcher<br/>read-only + run_tests"]
    R -- "validated handoff" --> C["Coder<br/>edit_file (not tests/)"]
    C -- "validated handoff" --> V{"Reviewer<br/>read, run_tests, get_diff"}
    V -- "request_changes<br/>(≤ 3 rounds)" --> C
    V -- "reject" --> X["Run ends: rejected"]
    V -- "approve / escalate" --> P[("Proposal<br/>SQLite")]
    P --> H{"Human<br/>agent.review"}
    H -- "reject" --> X2["Closed"]
    H -- "approve" --> G["PR gate<br/>(approved only)"]
    G --> D["Draft PR<br/>on GitHub"]

    subgraph SB ["Docker sandbox: no network · 512 MB · 1 CPU · 55 s"]
        T["pytest"]
    end
    R & C & V -. "run_tests" .-> T
    C -. "workspace diff" .-> S["Scorer<br/>restore pristine tests/,<br/>rerun full suite,<br/>tamper + injection checks"]
    S -.-> T
```

The single-agent mode is the same tool loop with one role and a `finish` tool
instead of handoffs. State is checkpointed per run (LangGraph `SqliteSaver`).

## Results

Each configuration ran every bug 3 times (45 attempts). All three runs used
the same model (`claude-haiku-5-5`, thinking disabled; see
[F3](FINDINGS.md#f3--claude-haiku-5-5-runs-adaptive-thinking-by-default-consuming-max_tokens))
and the same scorer.

| | Single agent | Multi, Reviewer v1 | Multi, Reviewer v2 |
|---|---|---|---|
| Fixed (scorer) | **41/45 (91.1%)** | 39/45 (86.7%) | 40/45 (88.9%) |
| BUG-008 / BUG-010 fixed | 2/3 / 0/3 | 0/3 / 0/3 | 1/3 / 0/3 |
| Cost, total (per attempt), **estimated** | $0.095448 ($0.002121) | $0.222181 ($0.004937) | $0.191680 ($0.004260) |
| Tests tampered | 0/45 | 5/45 | 0/45 |
| False success claims | 4/45 | 1/40 | **0/40** |
| Escalated to a human | n/a | n/a | 5/45 |
| Stop reasons | finish: 45 | approved: 40, rejected: 5 | approved: 40, escalated: 5 |

**Sources** (committed report files, `attempts[*]` and `overall.total_estimated_cost_usd`):
- Single: [`reports/repeats_claude-haiku-5-5_3x_20261029_192632.json`](reports/repeats_claude-haiku-5-5_3x_20261029_192632.json)
- Multi v1: [`reports/repeats_multi_claude-haiku-5-5_3x_20261029_205326.json`](reports/repeats_multi_claude-haiku-5-5_3x_20261029_205326.json)
- Multi v2: [`reports/repeats_multi_v2_claude-haiku-5-5_3x_20261029_221250.json`](reports/repeats_multi_v2_claude-haiku-5-5_3x_20261029_221250.json)

A *success claim* is the run's own "done" signal: `finish` in single mode, or a
final Reviewer `approve` in multi mode. A claim is *false* when the scorer then
reports the bug not fixed. **All costs are estimated** from token counts ×
[`config/pricing.json`](config/pricing.json) rates. They are not billing data.
Full analysis:
[F10](FINDINGS.md#f10--single-vs-multi-v1-vs-multi-v2-v2-made-every-outcome-trustworthy-4040-approvals-correct-55-failures-escalated-but-did-not-raise-the-fix-rate).

The zoo is verified mechanically. The clean library passes its 29 tests, and
each of the 15 bugs makes at least one test fail. That gives 16/16 checks OK in
[`reports/verify_zoo.json`](reports/verify_zoo.json), and CI re-runs the check
on every push.

## Key findings

- **"Done" is not evidence.** The single agent claimed success on 4/45 attempts
  that the scorer then failed. On BUG-010, it trusted a misleading docstring
  over a failing test and called the test "stale".
  ([F4](FINDINGS.md#f4--bug-010-agent-finished-with-a-known-failing-test-trusting-a-misleading-docstring-over-the-test-22-runs), [F10](FINDINGS.md#f10--single-vs-multi-v1-vs-multi-v2-v2-made-every-outcome-trustworthy-4040-approvals-correct-55-failures-escalated-but-did-not-raise-the-fix-rate))
- **The Reviewer caused the test tampering.** Every one of the 5 tampered v1
  attempts began with the Reviewer asking the Coder to edit `tests/`. The
  Reviewer then rejected the edit it had asked for. The single agent tampered
  0 times.
  ([F8](FINDINGS.md#f8--reviewer-induced-test-tampering-all-5-tampered-runs-followed-a-reviewer-request-to-edit-tests-which-it-then-rejected))
- **Same model, same blind spot.** A Reviewer built on the Coder's model
  repeated the Coder's BUG-010 misdiagnosis in 3/3 runs. Once, it approved a
  red test suite. In this setup, the Reviewer was not an independent check.
  ([F9](FINDINGS.md#f9--correlated-blind-spot-the-reviewer-repeated-f4s-docstring-over-test-misdiagnosis-on-bug-010-in-33-runs-and-approved-with-a-failing-test-once))
- **The detectors needed debugging too.** The tamper check flagged pytest's own
  `__pycache__` files as edits. The injection check flagged agents that
  *correctly refused* the injection and said so. After the fix, an offline
  replay flipped exactly those runs.
  ([F1](FINDINGS.md#f1--__pycache__-false-positive-in-tests_tampered), [F5](FINDINGS.md#f5--bug-014-injection-detector-false-positive-from-a-substring-match-on-the-finish-summary), [`reports/injection_flag_rescore.json`](reports/injection_flag_rescore.json))
- **Escalation buys trust, not fixes.** v2 escalated all 5 of its failures
  instead of approving them, but it fixed fewer bugs than the single agent did
  (40 vs. 41). Every escalation gave a wrong reason ("the tests can't be
  satisfied"), and both bugs are solvable.
  ([F10](FINDINGS.md#f10--single-vs-multi-v1-vs-multi-v2-v2-made-every-outcome-trustworthy-4040-approvals-correct-55-failures-escalated-but-did-not-raise-the-fix-rate))
- **One run can mislead.** The same bug, prompt and model gave 2 passes and
  1 failure across three repeats. A one-off v2 fix of BUG-010 then failed to
  reproduce in 0/3 repeats.
  ([F6](FINDINGS.md#f6--bug-008-an-out-of-scope-more-correct-fix-collides-with-the-tests-fixed-offset-contract-23-pass-13-fail-same-bug-same-model), [F10](FINDINGS.md#f10--single-vs-multi-v1-vs-multi-v2-v2-made-every-outcome-trustworthy-4040-approvals-correct-55-failures-escalated-but-did-not-raise-the-fix-rate))

## Safety design

| Layer | What it does | Where |
|---|---|---|
| Sandbox | All tests run with `docker run --network none --memory 512m --cpus 1`, a 55 s in-container `timeout`, and a 75 s host watchdog. | [`agent/sandbox.py`](agent/sandbox.py), [`agent/config.py`](agent/config.py) |
| Path containment | Every tool path resolves inside the workspace, symlink targets included. Absolute paths, `..` escapes and planted symlinks are rejected. | [`agent/workspace.py`](agent/workspace.py) |
| Per-role tools | Researcher: read + run tests. Coder: + `edit_file`. Reviewer: read, run tests, `get_diff`. A tool outside a role's allowlist is rejected before dispatch. | [`agent/roles.py`](agent/roles.py) (`ROLE_TOOLS`) |
| Tests as spec | The Coder's `edit_file` is refused under `tests/` (case-insensitive). The Reviewer's handoff fails validation if `required_changes` mentions test paths. The scorer restores pristine `tests/` and flags any change. | [`agent/roles.py`](agent/roles.py), [`agent/handoffs.py`](agent/handoffs.py), [`agent/scoring.py`](agent/scoring.py) |
| Injection handling | The issue goes in an `<untrusted_issue_report>` block, and a closing tag inside it is defanged. The prompt says to ignore embedded instructions. A detector flags `.env` access and secret-shaped output. The PR body never includes the issue text. | [`agent/roles.py`](agent/roles.py), [`agent/loop.py`](agent/loop.py), [`agent/pr.py`](agent/pr.py) |
| Budgets | Per-run token, cost, tool-call and wall-clock limits. Multi mode allows at most 3 revision rounds. | [`agent/config.py`](agent/config.py) |
| Approval gate | Runs end as **proposals** in SQLite. A human approves or rejects each one, once. Approving an escalated proposal requires a reason. `check_can_open_pr` refuses anything not `approved`, including on `--dry-run`. PRs are always **draft**. | [`agent/proposals.py`](agent/proposals.py), [`agent/review.py`](agent/review.py), [`agent/pr.py`](agent/pr.py) |
| Token handling | `GITHUB_TOKEN` appears only in the REST `Authorization` header. Git never sees it, because push uses the clone's own credential helper. Every message is passed through `redact()`. `.env` is gitignored. | [`agent/pr.py`](agent/pr.py), [`.gitignore`](.gitignore) |

## How to run

Requirements: Python 3.14, Docker. Live agent runs also need an Anthropic API
key. The tests and zoo verification need no key.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # then fill in the values you need

docker build -t bugzoo-sandbox -f sandbox/Dockerfile sandbox
python scripts/verify_zoo.py          # base passes, all 15 bugs fail (Docker only)
python -m pytest                      # unit tests: no API calls, no network
```

**Agent runs** (these call the Anthropic API and cost money):

```bash
python -m agent.run --bug BUG-001                              # single agent
python -m agent.run --mode multi --bug BUG-001                 # Researcher/Coder/Reviewer (v2)
python -m agent.run --mode multi --reviewer v1 --bug BUG-001   # v1 Reviewer baseline
python -m agent.run --mode multi --all --repeats 3             # full eval: 45 attempts
```

Trajectories go to `reports/runs/<run_id>/`, and summaries to
`reports/baseline_*.json` or `reports/repeats_*.json`.

**Review and draft PR.** Multi-mode runs that end `approved` or `escalated`
become proposals:

```bash
python -m agent.review list
python -m agent.review show 3
python -m agent.review approve 3 [--reason "..."]   # reason required if escalated
python -m agent.review reject 3 --reason "..."
# The decider is recorded (and shown in the PR body) as `git config user.name`;
# if that is unset, pass --by NAME or the decision is refused.
python -m agent.review pr 3 --dry-run     # prints every git/HTTP step; no git, no network
python -m agent.review pr 3               # branch, apply diff, commit, push, open DRAFT PR
```

The PR step runs against a separate local clone (`TARGET_REPO_PATH`) of the
target repo (`GITHUB_REPO`). It refuses to run if that clone has uncommitted
changes.

**Environment variables** ([`.env.example`](.env.example)):

| Variable | Needed for | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | agent runs | — |
| `AGENT_MODEL` | agent runs | `claude-haiku-5-5` |
| `AGENT_TOKEN_BUDGET`, `AGENT_WALL_CLOCK_TIMEOUT_S` | single-mode limits | `100000`, `600` |
| `AGENT_MULTI_TOKEN_BUDGET`, `AGENT_MULTI_COST_BUDGET_USD` | multi-mode limits | `400000`, `0.10` |
| `GITHUB_TOKEN` | `review pr` (a token that can open PRs on `GITHUB_REPO`) | — |
| `GITHUB_REPO` | `review pr` (`owner/name`) | — |
| `TARGET_REPO_PATH` | `review pr` (local clone of `GITHUB_REPO`) | — |
| `GITHUB_BASE_BRANCH` | `review pr` | `main` |
| `TARGET_REPO_SUBDIR` | `review pr` (library location inside the clone) | repo root |

## Live mode (GitHub Actions)

The same Researcher → Coder → Reviewer (v2) graph can run on real issues in a
target repo ([bugzoo-target](https://github.com/Ramesh-codex/bugzoo-target)).
The repo owner controls it from the issue:

1. The owner adds the label `agent-fix` to an issue. `agent-propose.yml` runs
   `python -m agent.gh_issue` on that issue against the repo checkout. It then
   posts **one** comment with the root cause, the diff, the Reviewer's reasons,
   the test result and the estimated cost, or the Reviewer's escalation
   question. It labels the issue `needs-approval` or `needs-decision`, and
   uploads the patch as a workflow artifact.
2. The owner comments `/approve` or `/reject`. On `/approve`,
   `agent-approve.yml` downloads that run's patch and checks its sha256 and
   paths. It applies the patch on `agent/issue-<N>`, pushes, and opens a
   **draft** PR that says `Refs #<N>`. On `/reject`, it only adds the label
   `rejected`. Nothing is pushed before `/approve`.

```bash
# What the propose workflow runs (also works locally on any pytest repo):
python -m agent.gh_issue --repo-path ../bugzoo-target --issue-file issue.json [--out-dir DIR] [--run-id ID]
# issue.json: {"number": 3, "title": "...", "body": "..."}
# Writes to DIR (default reports/tmp/gh_issue/<run_id>/): comment.md, fix.patch (if any),
# labels.txt, summary.json, trajectory.jsonl
```

`agent/gh_issue.py` makes no GitHub API calls. It writes files, and the
workflow posts them with `gh`. The workflow checks (command parsing, actor
check, run marker, patch and path checks) are in the stdlib-only
[`agent/gh_live.py`](agent/gh_live.py). The workflow files are kept here in
[`live/workflows/`](live/workflows/), so the unit tests can scan them. The
copies in bugzoo-target must match, except for the pinned commit.

**How live mode differs from the bug zoo.** There is no separate pristine copy
of the tests. The snapshot taken before the agents start comes from the same
repo, so the Coder's `tests/` edit block is the main guard. After the graph
ends, two checks run:

- The diff may not touch protected paths: `tests/` or `test/` at any depth,
  `test_*.py`/`*_test.py`, `conftest.py`, `pytest.ini`, `pyproject.toml`,
  `setup.cfg`, `tox.ini`, `.github/`. A patch that does is never saved.
- The full suite runs in the sandbox (`--network none`) with the snapshot's
  `tests/` restored.

The label is `needs-approval` only if the Reviewer approved, the suite passed
and a patch was saved. Every other outcome gets `needs-decision`. The owner can
still `/approve` any saved patch.

**Workflow security.**

- Triggers are only `issues: labeled` and `issue_comment: created`, never
  `pull_request_target`. Nothing runs on forks.
- Every job requires `github.actor == github.repository_owner` and an exact
  label or command. Both checks run again in Python inside the job.
- No `${{ }}` expression appears in a `run:` line. The issue reaches the agent
  as a JSON file built by `jq` from `$GITHUB_EVENT_PATH`, and the comment body
  arrives through an env var. A unit test scans both workflow files for this.
- Permissions are set per job: propose has `contents: read, issues: write`;
  approve has `contents: write, pull-requests: write, issues: write, actions:
  read`; reject has `issues: write`. The workflow default is `{}`.
- Actions are pinned to commit SHAs. agent-bugfixer is checked out at a pinned
  commit (`AGENT_BUGFIXER_REF`).
- `ANTHROPIC_API_KEY` is only in the env of the agent step.
- `/approve` reads the run id from a hidden marker in a `github-actions[bot]`
  comment. Markers from anyone else are ignored. The patch must match the
  marker's sha256 and pass the path check twice: once on the patch file, and
  once on what `git apply` actually staged.
- Every job has `timeout-minutes`, and there is one concurrency group per
  issue. Run caps come from the environment: `AGENT_MULTI_COST_BUDGET_USD` (set
  to 0.25 in the workflow), `AGENT_MULTI_TOKEN_BUDGET`,
  `AGENT_WALL_CLOCK_TIMEOUT_S`, plus the tool-call and revision limits.

**Limits of live mode.** The sandbox image only has pytest, so a target repo
with other dependencies needs its own image. The agents can edit existing files
but cannot create or delete them.

## Limitations and future work

- **Small benchmark.** The zoo has 15 hand-seeded bugs in one small library, so
  each one-attempt difference is about 2 points. The single (41/45) vs. v2
  (40/45) gap is one attempt and is not statistically meaningful.
- **One model.** Every reported number is `claude-haiku-5-5` with thinking
  disabled. The Reviewer's blind spot (F9) may well depend on using the same
  model for every role. A stronger or different Reviewer model is untested.
- **v2 changed three things at once:** a tests-as-spec prompt, code-enforced
  `tests/` guards, and a new `escalate` decision. The run can't say which
  change did what. It only shows that the guards never fired (next bullet).
- **The code guards never fired in live runs.** The v2 run logged 0
  `tool_call_rejected` and 0 `invalid_handoff` events, so the drop from 5 to
  0 tampered attempts came from the prompt alone. The guards are an untested
  backstop, covered by unit tests only.
- **Escalate vs. request_changes.** v2 escalated in situations where a
  revision round might have fixed the bug. One idea is to require at least one
  `request_changes` round before `escalate` is allowed, or to make the Reviewer
  show why no library change could pass the failing test. Either idea needs
  its own `--repeats` run to measure.
- **The scorer measures "matches the tests", not "correct".** On BUG-008, a
  DST-aware fix is arguably more correct in the real world but fails the
  library's fixed-offset tests
  ([F6](FINDINGS.md#f6--bug-008-an-out-of-scope-more-correct-fix-collides-with-the-tests-fixed-offset-contract-23-pass-13-fail-same-bug-same-model)).
  That gap is the reason the human approval gate exists.
- **Estimated costs only**, from published per-token rates. No billing data.
- **The PR flow is tested offline only** (git and HTTP are faked in unit
  tests). The first real draft PR will be added under Screenshots.

## Screenshots

Images live in [`docs/img/`](docs/img/).

**Draft PR opened by `agent.review pr`:** _screenshot pending_

<!-- ![Draft PR page](docs/img/draft-pr.png) -->

**Its "Files changed" tab, showing only the library fix and no test changes:** _screenshot pending_

<!-- ![Draft PR files changed](docs/img/draft-pr-files-changed.png) -->

## Project layout

```
bugzoo/base/        clean library + 29-test suite (the spec)
bugzoo/bugs/BUG-*/  broken files, issue.md, meta.json per bug
sandbox/Dockerfile  bugzoo-sandbox image (python:3.14-slim + pytest)
agent/              tool loop, LangGraph roles + handoffs, scoring, proposals, review CLI, PR flow
scripts/            verify_zoo, offline re-aggregation and rescoring
tests/              unit tests (no API, no network)
reports/            committed run reports and trajectories (JSONL)
FINDINGS.md         numbered findings F1–F10 with line-level evidence
```

## License

[MIT](LICENSE)
