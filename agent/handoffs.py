"""Validated handoff objects passed between the multi-agent roles.

Each role ends its turn by calling a submit tool whose arguments must
validate against one of these models (extra="forbid", like every tool
schema). Only the validated, model_dump()'d dict goes into graph state --
plain JSON, so the SQLite checkpointer can persist it without custom serde.
"""

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


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "request_changes", "reject"]
    reasons: list[str] = Field(min_length=1)
    required_changes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _changes_required_when_requested(self) -> "ReviewDecision":
        if self.decision == "request_changes" and not self.required_changes:
            raise ValueError("request_changes needs at least one entry in required_changes")
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
