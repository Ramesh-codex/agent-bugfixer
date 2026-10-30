# agent-bugfixer

Evaluates a coding agent against a "bug zoo": a small Python library with
15 seeded bugs (2 adversarial: prompt-injection, test-tampering), run in a
Docker sandbox (no network, 512MB, 1 cpu, timeout).

## Milestones
- **M1 Bug zoo** ✓ — `bugzoo/`, `sandbox/Dockerfile`, `scripts/verify_zoo.py`.
- **M2 Single-agent loop** ✓ — `agent/` tool loop + baseline measurement.
- **M3 LangGraph Researcher/Coder/Reviewer** ✓ — `agent/graph.py`, `roles.py`, `handoffs.py`; `--mode multi`, `--reviewer v1|v2` (v2 default). Result: FINDINGS.md F10.
- **M4 Approval gate + draft PR** — built: `agent/proposals.py` (SQLite `reports/proposals.sqlite`), `agent/review.py` (CLI), `agent/pr.py` (gate, git, GitHub REST). Offline-tested; no real PR opened yet (user does that).
- **M5 Portfolio packaging** — built: README.md, .env.example, LICENSE (MIT), `.github/workflows/ci.yml` (unit tests + verify_zoo, no secrets). Not pushed; screenshots in `docs/img/` pending.
- **M6 Live mode (GitHub Actions)** — built: `agent/gh_issue.py` (entry point, writes comment/patch/labels/summary files, no GitHub API), `agent/gh_live.py` (stdlib-only workflow checks), `live/workflows/agent-{propose,approve}.yml` (canonical copies; bugzoo-target holds identical ones except `AGENT_BUGFIXER_REF`). Not committed/pushed; secrets, labels and settings are the user's.

## Layout
- `bugzoo/base/` clean library + tests. `bugzoo/bugs/BUG-*/` broken files + issue.md + meta.json.
- `sandbox/Dockerfile` — `bugzoo-sandbox` image. `scripts/verify_zoo.py` — asserts every bug fails, base passes.
- `agent/` — config, workspace (path containment), sandbox (docker runner), tools (Pydantic schemas), loop, scoring, run (CLI), aggregate. Multi-agent: graph (LangGraph + SqliteSaver), roles (prompts, per-role tool permissions, Reviewer v1/v2), handoffs (validated handoff models).
- `config/pricing.json` — per-model $/MTok, tiered where applicable.
- `tests/` — unit tests, no API calls. `reports/` — trajectory logs + `baseline_<mode>[_<reviewer>]_<model>_<run_id>.json` / `repeats_..._<N>x_<run_id>.json` (`tmp/` gitignored; pre-M3 reports have no mode in the name).

## Commands
- Build sandbox: `docker build -t bugzoo-sandbox -f sandbox/Dockerfile sandbox`
- Verify zoo: `python scripts/verify_zoo.py`
- Unit tests: `python -m pytest` (repo root)
- Agent, one bug: `python -m agent.run --bug BUG-001` (single) / `python -m agent.run --mode multi --bug BUG-001` (Reviewer v2; `--reviewer v1` for the M3 baseline)
- Re-aggregate a repeats report offline: `python -m scripts.regenerate_repeats_aggregate <report.json>`
- Proposals (created by multi runs that end approved/escalated): `python -m agent.review list | show <id> | approve <id> [--reason] | reject <id> --reason "..."`
- Draft PR (approved only): `python -m agent.review pr <id> [--dry-run]` — needs `GITHUB_REPO`, `GITHUB_TOKEN`, `TARGET_REPO_PATH` (local clone) in .env; optional `GITHUB_BASE_BRANCH` (main), `TARGET_REPO_SUBDIR`
- Live mode, one issue (1 multi-agent run): `python -m agent.gh_issue --repo-path <dir> --issue-file <json> [--out-dir DIR]`
- Agent, all bugs: `python -m agent.run --all` — **never without my OK, see Rules**

## Rules
1. Never read, print, `cat`, or otherwise surface the contents of `.env` — in code, in chat, or in logs.
2. Never run `--all` or any multi-bug live run (= multiple API calls) without my explicit OK first.
3. One milestone at a time — don't start the next until I say to move on.
4. Verify model IDs against `client.models.list()` before trusting cached or trained knowledge — new models ship after any training cutoff.
5. Every metric (cost, tokens, pass/fail, timing) must come from a real run — never present an estimate as measured, or vice versa.

## Findings
- `tests_tampered` false positive: pytest's own `__pycache__`/`.pytest_cache` files under `tests/` were compared as if the agent had edited them. Fixed in `agent/scoring.py` by excluding cache/bytecode paths from the diff.
- Docker cold-start: the first container run in a session is ~8s slower than steady-state. `agent/run.py` runs one warm-up container before timing any bug.
- `claude-haiku-5-5` runs adaptive thinking by default (4.5 didn't), and thinking tokens count against `max_tokens`. The loop sends `thinking: {"type": "disabled"}` explicitly so results stay comparable to the 4.5 baseline — current baseline = thinking disabled, not the model's default behavior.
