"""Milestone 4: fix proposals awaiting a human decision.

After a multi-mode run ends with the Reviewer approving or escalating, the
result is saved here as a proposal (status pending). A human then approves
or rejects it with `python -m agent.review`; only an approved proposal can
become a draft PR (agent/pr.py). State lives in SQLite so it survives
restarts -- every call opens its own short-lived connection.

Escalated proposals are flagged needs_decision: the Reviewer found the tests
and the issue in conflict, and its reasons are the question for the human.
"""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from agent import config

STATUSES = ("pending", "approved", "rejected")
PROPOSAL_STOP_REASONS = ("approved", "escalated")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    attempt TEXT NOT NULL,           -- BUG-001 or BUG-001_rep2
    bug_id TEXT NOT NULL,
    category TEXT,
    model TEXT,
    reviewer_version TEXT,
    created_at TEXT NOT NULL,
    stop_reason TEXT NOT NULL,       -- approved | escalated
    needs_decision INTEGER NOT NULL,
    reviewer_question TEXT,
    changed_files TEXT NOT NULL,     -- JSON list
    diff TEXT NOT NULL,
    research TEXT,                   -- JSON: the Researcher's report
    review_decisions TEXT NOT NULL,  -- JSON list, every Reviewer decision in order
    tests_fixed INTEGER NOT NULL,
    tests_summary TEXT,
    tests_tampered INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by TEXT,
    decided_at TEXT,
    decision_reason TEXT,
    pr_branch TEXT,
    pr_url TEXT,
    UNIQUE (run_id, attempt)
)
"""

_JSON_FIELDS = ("changed_files", "research", "review_decisions")
_BOOL_FIELDS = ("needs_decision", "tests_fixed", "tests_tampered")


class ProposalError(Exception):
    """A refused proposal operation (unknown id, invalid transition, ...)."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def pytest_summary(output: str) -> str:
    """Last non-empty line of pytest output, e.g. '15 passed in 0.31s'."""
    lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
    return lines[-1].strip("= ") if lines else ""


class ProposalStore:
    def __init__(self, path: Path = config.PROPOSALS_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        p = dict(row)
        for f in _JSON_FIELDS:
            p[f] = json.loads(p[f]) if p[f] is not None else None
        for f in _BOOL_FIELDS:
            p[f] = bool(p[f])
        return p

    def save(
        self, *, run_id: str, attempt: str, bug_id: str, category: str | None, model: str,
        reviewer_version: str, stop_reason: str, changed_files: list[str], diff: str,
        research: dict | None, review_decisions: list[dict], tests_fixed: bool, tests_output: str,
        tests_tampered: bool, estimated_cost_usd: float,
    ) -> int:
        if stop_reason not in PROPOSAL_STOP_REASONS:
            raise ProposalError(f"no proposal for stop_reason {stop_reason!r}")
        needs_decision = stop_reason == "escalated"
        question = None
        if needs_decision and review_decisions:
            question = "\n".join(review_decisions[-1]["reasons"])
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO proposals (run_id, attempt, bug_id, category, model, reviewer_version,"
                " created_at, stop_reason, needs_decision, reviewer_question, changed_files, diff,"
                " research, review_decisions, tests_fixed, tests_summary, tests_tampered,"
                " estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, attempt, bug_id, category, model, reviewer_version, _now(), stop_reason,
                 int(needs_decision), question, json.dumps(changed_files), diff,
                 json.dumps(research) if research is not None else None, json.dumps(review_decisions),
                 int(tests_fixed), pytest_summary(tests_output), int(tests_tampered),
                 round(estimated_cost_usd, 6)),
            )
            return cur.lastrowid

    def get(self, proposal_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ProposalError(f"no proposal with id {proposal_id}")
        return self._decode(row)

    def list(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
        return [self._decode(r) for r in rows]

    def decide(self, proposal_id: int, status: str, decided_by: str, reason: str | None = None) -> dict:
        """pending -> approved | rejected, exactly once. Rejecting always needs
        a reason; so does approving an escalated (needs-decision) proposal,
        since the reason is the human's answer to the Reviewer's question."""
        if status not in ("approved", "rejected"):
            raise ProposalError(f"invalid decision {status!r}")
        if not decided_by.strip():
            raise ProposalError("decided_by must not be empty")
        proposal = self.get(proposal_id)
        if (status == "rejected" or proposal["needs_decision"]) and not (reason and reason.strip()):
            raise ProposalError(f"a --reason is required to {status[:-1]} proposal {proposal_id}"
                                + (" (it needs a decision)" if proposal["needs_decision"] else ""))
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "UPDATE proposals SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ?"
                " WHERE id = ? AND status = 'pending'",
                (status, decided_by, _now(), reason, proposal_id),
            )
        if cur.rowcount != 1:
            raise ProposalError(
                f"proposal {proposal_id} is already {proposal['status']} "
                f"(by {proposal['decided_by']} at {proposal['decided_at']}); decisions are final"
            )
        return self.get(proposal_id)

    def set_pr(self, proposal_id: int, branch: str, url: str | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE proposals SET pr_branch = ?, pr_url = COALESCE(?, pr_url) WHERE id = ?",
                         (branch, url, proposal_id))
