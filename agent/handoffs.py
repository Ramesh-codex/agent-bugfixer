"""Validated handoff objects passed between the multi-agent roles.

Each role ends its turn by calling a submit tool whose arguments must
validate against one of these models (extra="forbid", like every tool
schema). Only the validated, model_dump()'d dict goes into graph state --
plain JSON, so the SQLite checkpointer can persist it without custom serde.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str = Field(min_length=1, description="Workspace-relative path the quote is from.")
    quote: str = Field(min_length=1, description="Verbatim excerpt from that file.")


class ResearchReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suspected_files: list[str] = Field(min_length=1)
    root_cause: str = Field(min_length=1)
    failing_tests: list[str] = Field(description="Test node ids that fail because of the bug; may be empty.")
    evidence: list[Evidence] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]


class CoderHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, description="What you changed and why.")
    files_changed: list[str]


# Reviewer v1 handoff, kept unchanged so `--reviewer v1` reproduces the M3
# baseline (run 20261029_205326) -- including the tool schema the model sees,
# hence the pinned title and no class docstring (both would land in it).
class ReviewDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", title="ReviewDecision")
    decision: Literal["approve", "request_changes", "reject"]
    reasons: list[str] = Field(min_length=1)
    required_changes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _changes_required_when_requested(self):
        if self.decision == "request_changes" and not self.required_changes:
            raise ValueError("request_changes needs at least one entry in required_changes")
        return self


# A path under tests/ ("tests/x.py", "./tests/x", "`tests\\x`", "Tests/...")
# or a test module's file name ("test_dates.py") -- test modules live only
# under tests/. Test *function* names (test_foo) are allowed: naming the
# failing test is how the Reviewer says which behavior must change.
_TESTS_PATH_RE = re.compile(r"(?<![\w.-])(?:\./)?tests[/\\]|\btest_\w*\.py\b", re.IGNORECASE)


def references_tests_path(text: str) -> bool:
    return bool(_TESTS_PATH_RE.search(text))


# Reviewer v2 handoff: adds `escalate` (tests and issue conflict; a human must
# decide), and required_changes may never point at tests/ (FINDINGS.md F8:
# every tampered multi-agent run started with the Reviewer asking for a test
# edit). No class docstring, for the same reason as v1.
class ReviewDecision(ReviewDecisionV1):
    decision: Literal["approve", "request_changes", "reject", "escalate"]

    @model_validator(mode="after")
    def _no_test_changes_required(self):
        offending = [c for c in self.required_changes if references_tests_path(c)]
        if offending:
            raise ValueError(
                "required_changes must not reference tests/ paths or test files -- the tests are "
                "the specification and are never changed. Name the failing test function and "
                f"describe the library change instead. Offending entries: {offending}"
            )
        return self


def _inline_refs(node, defs: dict):
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def handoff_tool_definition(name: str, description: str, model: type[BaseModel]) -> dict:
    """Anthropic tool definition whose input schema IS the Pydantic model's
    (with nested $defs inlined, so no $ref resolution is needed)."""
    schema = model.model_json_schema()
    schema = _inline_refs(schema, schema.get("$defs", {}))
    schema["additionalProperties"] = False
    return {"name": name, "description": description, "input_schema": schema}
