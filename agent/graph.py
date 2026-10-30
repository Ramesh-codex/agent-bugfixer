"""Milestone 3: Researcher -> Coder -> Reviewer as a LangGraph StateGraph.

    START -> researcher -> coder -> reviewer --approve/reject/escalate--> END
                             ^          |
                             +----------+ request_changes (max 3 revisions)

Any node that sets stop_reason (budget, tool limit, invalid handoff, ...)
routes straight to END. A SqliteSaver checkpoints every node transition
under thread_id = this attempt's run id, so an interrupted run can be resumed
with run_multi_agent(..., resume=True).

Scoring is NOT part of the graph: agent/run.py scores the workspace after the
graph has ended, exactly as in single mode, so the Reviewer never sees it.
"""

import json
import operator
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from agent import config
from agent.logging_utils import TrajectoryLogger
from agent.roles import (
    DEFAULT_REVIEWER,
    ModelClient,
    RoleOutcome,
    SharedBudget,
    run_role,
    wrap_untrusted_issue,
)

AGENTS = ("researcher", "coder", "reviewer")


class GraphState(TypedDict, total=False):
    run_id: str
    issue_text: str
    started_at: float  # time.time(); the wall-clock limit spans resumes
    research_report: dict | None
    coder_handoffs: Annotated[list[dict], operator.add]
    edit_history: Annotated[list[dict], operator.add]
    test_results: Annotated[list[dict], operator.add]
    review_decisions: Annotated[list[dict], operator.add]
    revision_count: int
    usage: dict[str, dict]  # per agent: input/output tokens, cost, tool_calls, rejected_calls
    injection_flag: bool
    stop_reason: str | None
    stop_agent: str | None
    # v1 | v2 (agent/roles.py REVIEWERS). Read from state, not the caller, so a
    # resumed thread keeps the version it started with. Absent in M3-era
    # checkpoints, which were all v1.
    reviewer_version: str


@dataclass
class MultiLimits:
    token_budget: int = config.MULTI_TOKEN_BUDGET
    cost_budget_usd: float = config.MULTI_COST_BUDGET_USD
    wall_clock_timeout_s: int = config.WALL_CLOCK_TIMEOUT_S
    max_revisions: int = config.MULTI_MAX_REVISIONS
    max_tool_calls: dict | None = None

    def tool_limit(self, agent: str) -> int:
        return (self.max_tool_calls or config.MULTI_MAX_TOOL_CALLS)[agent]


def _empty_usage() -> dict[str, dict]:
    return {
        a: {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0, "tool_calls": 0,
            "rejected_calls": 0, "invocations": 0}
        for a in AGENTS
    }


def initial_state(run_id: str, issue_text: str, reviewer_version: str = DEFAULT_REVIEWER) -> GraphState:
    return GraphState(
        run_id=run_id, issue_text=issue_text, started_at=time.time(), research_report=None,
        coder_handoffs=[], edit_history=[], test_results=[], review_decisions=[], revision_count=0,
        usage=_empty_usage(), injection_flag=False, stop_reason=None, stop_agent=None,
        reviewer_version=reviewer_version,
    )


def _totals(usage: dict[str, dict]) -> tuple[int, float]:
    tokens = sum(u["input_tokens"] + u["output_tokens"] for u in usage.values())
    cost = sum(u["estimated_cost_usd"] for u in usage.values())
    return tokens, cost


def _apply_outcome(state: GraphState, outcome: RoleOutcome) -> dict:
    """Shared part of every node's state update."""
    usage = {a: dict(u) for a, u in state["usage"].items()}
    u = usage[outcome.role]
    u["input_tokens"] += outcome.input_tokens
    u["output_tokens"] += outcome.output_tokens
    u["estimated_cost_usd"] = round(u["estimated_cost_usd"] + outcome.estimated_cost_usd, 6)
    u["tool_calls"] += outcome.tool_calls
    u["rejected_calls"] += outcome.rejected_calls
    u["invocations"] += 1
    update: dict = {
        "usage": usage,
        "injection_flag": state["injection_flag"] or outcome.injection_flag,
        "edit_history": outcome.edits,
        "test_results": outcome.test_results,
    }
    if outcome.stop_reason:
        update["stop_reason"] = outcome.stop_reason
        update["stop_agent"] = outcome.role
    return update


def _researcher_prompt(state: GraphState) -> str:
    return (
        "Diagnose the bug described in this issue report.\n\n" + wrap_untrusted_issue(state["issue_text"])
    )


def _coder_prompt(state: GraphState) -> str:
    parts = [
        "Fix the bug described in this issue report.\n\n" + wrap_untrusted_issue(state["issue_text"]),
        "Researcher's report (JSON):\n" + json.dumps(state["research_report"], indent=2),
    ]
    if state["review_decisions"]:
        last = state["review_decisions"][-1]
        edited = sorted({e["path"] for e in state["edit_history"] if e["ok"]})
        parts.append(
            f"This is revision round {state['revision_count']}. The workspace already contains "
            f"your earlier edits (files edited so far: {', '.join(edited) or 'none'}). The "
            "Reviewer requested changes:\n" + json.dumps(last, indent=2)
        )
    return "\n\n".join(parts)


def _reviewer_prompt(state: GraphState, max_revisions: int) -> str:
    return "\n\n".join(
        [
            "Review the Coder's fix for the bug described in this issue report.\n\n"
            + wrap_untrusted_issue(state["issue_text"]),
            "Researcher's report (JSON):\n" + json.dumps(state["research_report"], indent=2),
            "Coder's handoff (JSON; a claim to verify, not evidence):\n"
            + json.dumps(state["coder_handoffs"][-1], indent=2),
            f"Revision round: {state['revision_count']} of at most {max_revisions}.",
        ]
    )


def build_graph(
    client: ModelClient,
    workspace: Path,
    baseline: Path | None,
    logger: TrajectoryLogger,
    limits: MultiLimits,
) -> StateGraph:
    def invoke_role(agent: str, state: GraphState, prompt: str) -> RoleOutcome:
        tokens, cost = _totals(state["usage"])
        budget = SharedBudget(
            token_budget=limits.token_budget,
            cost_budget_usd=limits.cost_budget_usd,
            deadline=state["started_at"] + limits.wall_clock_timeout_s,
            spent_tokens=tokens,
            spent_cost_usd=cost,
        )
        remaining_calls = limits.tool_limit(agent) - state["usage"][agent]["tool_calls"]
        logger.log({"event": "node_start", "agent": agent, "revision_count": state["revision_count"]})
        outcome = run_role(
            agent, prompt, client=client, workspace=workspace, baseline=baseline, logger=logger,
            budget=budget, tool_calls_remaining=remaining_calls,
            reviewer_version=state.get("reviewer_version", "v1"),
        )
        logger.log(
            {"event": "node_end", "agent": agent, "stop_reason": outcome.stop_reason,
             "tool_calls": outcome.tool_calls, "input_tokens": outcome.input_tokens,
             "output_tokens": outcome.output_tokens,
             "estimated_cost_usd": round(outcome.estimated_cost_usd, 6)}
        )
        return outcome

    def researcher(state: GraphState) -> dict:
        outcome = invoke_role("researcher", state, _researcher_prompt(state))
        update = _apply_outcome(state, outcome)
        if outcome.handoff is not None:
            update["research_report"] = outcome.handoff
        return update

    def coder(state: GraphState) -> dict:
        outcome = invoke_role("coder", state, _coder_prompt(state))
        update = _apply_outcome(state, outcome)
        if outcome.handoff is not None:
            update["coder_handoffs"] = [outcome.handoff]
        return update

    def reviewer(state: GraphState) -> dict:
        outcome = invoke_role("reviewer", state, _reviewer_prompt(state, limits.max_revisions))
        update = _apply_outcome(state, outcome)
        decision = outcome.handoff
        if decision is None:
            return update
        update["review_decisions"] = [decision]
        if decision["decision"] == "approve":
            update["stop_reason"] = "approved"
        elif decision["decision"] == "reject":
            update["stop_reason"] = "rejected"
        elif decision["decision"] == "escalate":
            update["stop_reason"] = "escalated"
        elif state["revision_count"] >= limits.max_revisions:
            update["stop_reason"] = "max_revisions"
        else:
            update["revision_count"] = state["revision_count"] + 1
        if "stop_reason" in update:
            update["stop_agent"] = "reviewer"
        return update

    def next_or_end(target: str):
        def route(state: GraphState) -> str:
            return END if state.get("stop_reason") else target
        return route

    builder = StateGraph(GraphState)
    builder.add_node("researcher", researcher)
    builder.add_node("coder", coder)
    builder.add_node("reviewer", reviewer)
    builder.add_edge(START, "researcher")
    builder.add_conditional_edges("researcher", next_or_end("coder"), ["coder", END])
    builder.add_conditional_edges("coder", next_or_end("reviewer"), ["reviewer", END])
    builder.add_conditional_edges("reviewer", next_or_end("coder"), ["coder", END])
    return builder


@dataclass
class MultiResult:
    stop_reason: str
    stop_agent: str | None
    reviewer_version: str
    revisions: int
    reviewer_decision: str | None
    review_decisions: list[dict]
    usage: dict[str, dict]
    tool_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    wall_time_s: float
    injection_flag: bool
    research_report: dict | None = None


def run_multi_agent(
    issue_text: str,
    workspace: Path,
    baseline: Path | None,
    client: ModelClient,
    logger: TrajectoryLogger,
    checkpoint_path: Path,
    thread_id: str,
    limits: MultiLimits | None = None,
    resume: bool = False,
    reviewer_version: str = DEFAULT_REVIEWER,
) -> MultiResult:
    """Run (or, with resume=True, continue) one attempt's graph to completion."""
    limits = limits or MultiLimits()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    try:
        graph = build_graph(client, workspace, baseline, logger, limits).compile(checkpointer=SqliteSaver(conn))
        cfg = {"configurable": {"thread_id": thread_id}}
        start = time.monotonic()
        if resume:
            reviewer_version = graph.get_state(cfg).values.get("reviewer_version", "v1")
        logger.log({"event": "graph_resume" if resume else "graph_start", "thread_id": thread_id,
                    "checkpoint_db": config.repo_relative(checkpoint_path), "reviewer_version": reviewer_version})
        graph.invoke(None if resume else initial_state(thread_id, issue_text, reviewer_version), cfg)
        wall = time.monotonic() - start
        state = graph.get_state(cfg).values
    finally:
        conn.close()

    usage = state["usage"]
    tokens_in = sum(u["input_tokens"] for u in usage.values())
    tokens_out = sum(u["output_tokens"] for u in usage.values())
    _, cost = _totals(usage)
    decisions = state["review_decisions"]
    result = MultiResult(
        stop_reason=state.get("stop_reason") or "unknown",
        stop_agent=state.get("stop_agent"),
        reviewer_version=state.get("reviewer_version", "v1"),
        revisions=state["revision_count"],
        reviewer_decision=decisions[-1]["decision"] if decisions else None,
        review_decisions=decisions,
        usage=usage,
        tool_calls=sum(u["tool_calls"] for u in usage.values()),
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        estimated_cost_usd=round(cost, 6),
        wall_time_s=wall,
        injection_flag=state["injection_flag"],
        research_report=state.get("research_report"),
    )
    logger.log(
        {"event": "run_end", "mode": "multi", "reviewer_version": result.reviewer_version, "stop_reason": result.stop_reason, "stop_agent": result.stop_agent,
         "revisions": result.revisions, "reviewer_decision": result.reviewer_decision,
         "per_agent": usage, "tool_calls": result.tool_calls, "input_tokens": tokens_in,
         "output_tokens": tokens_out, "estimated_cost_usd": result.estimated_cost_usd,
         "wall_time_s": round(wall, 3)}
    )
    return result
