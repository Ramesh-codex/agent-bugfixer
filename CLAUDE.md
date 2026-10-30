# agent-bugfixer

Evaluates a coding agent against a "bug zoo": a small Python library with
15 seeded bugs (2 adversarial: prompt-injection, test-tampering), run in a
Docker sandbox (no network, 512MB, 1 cpu, timeout).

## Milestones
- **M1 Bug zoo** ✓ — `bugzoo/`, `sandbox/Dockerfile`, `scripts/verify_zoo.py`.
- **M2 Single-agent loop** ✓ — `agent/` tool loop + baseline measurement.
- **M3 LangGraph Researcher/Coder/Reviewer** — not started.
- **M4 Approval gate + draft PR** — not started.
- **M5 Full eval + findings** — not started.

## Layout
- `bugzoo/base/` clean library + tests. `bugzoo/bugs/BUG-*/` broken files + issue.md + meta.json.
- `sandbox/Dockerfile` — `bugzoo-sandbox` image. `scripts/verify_zoo.py` — asserts every bug fails, base passes.
- `agent/` — config, workspace (path containment), sandbox (docker runner), tools (Pydantic schemas), loop, scoring, run (CLI).
- `config/pricing.json` — per-model $/MTok, tiered where applicable.
- `tests/` — unit tests, no API calls. `reports/` — trajectory logs + `baseline_<model>_<run_id>.json` (`tmp/` gitignored).

## Commands
- Build sandbox: `docker build -t bugzoo-sandbox -f sandbox/Dockerfile sandbox`
- Verify zoo: `python scripts/verify_zoo.py`
- Unit tests: `python -m pytest` (repo root)
- Agent, one bug: `python -m agent.run --bug BUG-001`
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
