"""Environment and pricing configuration for the agent."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def repo_relative(path: Path) -> str:
    """`path` relative to the repo root, POSIX form, for logs and printed output --
    so they never record where the checkout lives. Paths outside the repo pass
    through unchanged."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-5-5")

MAX_TOOL_CALLS = 25
TOKEN_BUDGET = int(os.environ.get("AGENT_TOKEN_BUDGET", "100000"))
WALL_CLOCK_TIMEOUT_S = int(os.environ.get("AGENT_WALL_CLOCK_TIMEOUT_S", "600"))

# Multi-agent mode (--mode multi): ONE token/cost budget shared by all agents,
# plus a per-agent tool-call limit that applies to the whole run (a Coder
# invoked again for a revision keeps spending from the same allowance).
MULTI_TOKEN_BUDGET = int(os.environ.get("AGENT_MULTI_TOKEN_BUDGET", "400000"))
MULTI_COST_BUDGET_USD = float(os.environ.get("AGENT_MULTI_COST_BUDGET_USD", "0.10"))
MULTI_MAX_TOOL_CALLS = {"researcher": 15, "coder": 30, "reviewer": 20}
MULTI_MAX_REVISIONS = 3

SANDBOX_IMAGE = "bugzoo-sandbox"
SANDBOX_MEMORY = "512m"
SANDBOX_CPUS = "1"
CONTAINER_TEST_TIMEOUT_S = 55  # enforced inside the container via coreutils `timeout`
SUBPROCESS_TEST_TIMEOUT_S = 75  # outer watchdog, must exceed CONTAINER_TEST_TIMEOUT_S

PRICING_PATH = ROOT / "config" / "pricing.json"

# M4 approval gate + draft PRs (agent/proposals.py, agent/pr.py, agent/review.py).
PROPOSALS_DB = ROOT / "reports" / "proposals.sqlite"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # never log or print this
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # "owner/name"
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")
TARGET_REPO_PATH = os.environ.get("TARGET_REPO_PATH")  # local clone of GITHUB_REPO
# Where the bug-zoo library lives inside that clone ("" = repo root).
TARGET_REPO_SUBDIR = os.environ.get("TARGET_REPO_SUBDIR", "")


def load_pricing() -> dict:
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))


def model_pricing(model: str) -> dict:
    """Raw pricing.json entry for `model` -- either a flat {input_per_mtok,
    output_per_mtok} dict, or {"tiers": [...]} for a model priced per request
    by that request's own prompt size (e.g. claude-haiku-5-5)."""
    pricing = load_pricing()
    if model not in pricing:
        raise KeyError(
            f"No pricing entry for model {model!r} in {PRICING_PATH}. "
            "Add one before running the agent so costs can be estimated."
        )
    return pricing[model]


def _select_tier(tiers: list[dict], prompt_tokens: int) -> dict:
    """First tier (in pricing.json order) whose cap covers `prompt_tokens`,
    else the last (open-ended) tier."""
    for tier in tiers:
        cap = tier.get("max_prompt_tokens")
        if cap is None or prompt_tokens <= cap:
            return tier
    return tiers[-1]


def tier_label(model: str, input_tokens: int) -> str | None:
    """Which pricing tier a request with this many input tokens falls into,
    or None if `model` is not tiered (flat per-MTok rate)."""
    pricing = model_pricing(model)
    if "tiers" not in pricing:
        return None
    return _select_tier(pricing["tiers"], input_tokens)["label"]


def estimate_request_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """ESTIMATED USD cost of ONE request (not billing data), from
    config/pricing.json.

    For a tiered model (claude-haiku-5-5), Anthropic prices each request by
    that request's OWN prompt size: "a request over the threshold pays the
    higher prices even when part of its prompt is a cache hit, and earlier
    requests keep the prices they were billed at." So the tier is selected
    per call from `input_tokens`, never from totals accumulated across a run
    -- callers must invoke this once per request and sum the results, not
    call it once on a run's aggregate input/output totals.
    """
    pricing = model_pricing(model)
    rates = _select_tier(pricing["tiers"], input_tokens) if "tiers" in pricing else pricing
    cost = (input_tokens / 1_000_000) * rates["input_per_mtok"] + (
        output_tokens / 1_000_000
    ) * rates["output_per_mtok"]
    return round(cost, 6)
