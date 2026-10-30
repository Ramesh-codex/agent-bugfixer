"""Agent tools: strict Pydantic arg schemas + sandboxed implementations.

Every tool's arguments are validated against a Pydantic model with
extra="forbid" before anything runs, so unexpected or missing fields are
rejected outright. Every filesystem-touching tool resolves its path through
agent.workspace.resolve_in_workspace, which is the sole barrier keeping these
tools inside the workspace (see workspace.py for why).
"""

import difflib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent import sandbox
from agent.scoring import _relative_file_set
from agent.workspace import PathEscapeError, resolve_in_workspace

MAX_READ_CHARS = 100_000


class ToolError(Exception):
    """Raised for any tool-call rejection: path escape or filesystem failure.
    (Invalid *arguments* are rejected earlier, by Pydantic, in dispatch_tool.)"""


class ListDirArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = "."


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    path: str = "."
    max_results: int = Field(default=50, ge=1, le=500)


class EditFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    old_string: str
    new_string: str


class RunTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_path: str | None = None


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)


class GetDiffArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


TOOL_SCHEMAS: dict[str, type[BaseModel]] = {
    "list_dir": ListDirArgs,
    "read_file": ReadFileArgs,
    "search": SearchArgs,
    "edit_file": EditFileArgs,
    "run_tests": RunTestsArgs,
    "finish": FinishArgs,
    "get_diff": GetDiffArgs,
}

# The single-agent loop's tool set. get_diff is multi-agent only (Reviewer):
# it needs a baseline snapshot of the workspace, which only that mode makes.
SINGLE_AGENT_TOOLS = ("list_dir", "read_file", "search", "edit_file", "run_tests", "finish")


def tool_definitions(names: tuple[str, ...] | list[str] = SINGLE_AGENT_TOOLS) -> list[dict]:
    """Anthropic tool-use JSON schemas for `names` (default: the single-agent set)."""
    return [d for d in _all_tool_definitions() if d["name"] in names]


def _all_tool_definitions() -> list[dict]:
    return [
        {
            "name": "list_dir",
            "description": "List files and directories at a path relative to the workspace root.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the workspace root. Defaults to '.'.",
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "read_file",
            "description": "Read the full UTF-8 text contents of a file relative to the workspace root.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace root."}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search",
            "description": (
                "Plain substring search (not regex) for `query` across text files "
                "under `path`, recursively."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Literal substring to search for."},
                    "path": {
                        "type": "string",
                        "description": "Directory to search under, relative to the workspace root. Defaults to '.'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching lines to return. Defaults to 50.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "edit_file",
            "description": (
                "Replace exactly one occurrence of `old_string` with `new_string` in the "
                "file at `path`. Fails if `old_string` is missing or appears more than once "
                "-- include enough surrounding context in `old_string` to make it unique."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace root."},
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace. Must appear exactly once in the file.",
                    },
                    "new_string": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        },
        {
            "name": "run_tests",
            "description": (
                "Run the pytest suite inside the sandboxed container (no network, "
                "512MB memory, 1 cpu, time-limited). Optionally scope to one file or "
                "directory relative to the workspace root."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "test_path": {
                        "type": "string",
                        "description": "Optional file or directory to run instead of the full suite, e.g. 'tests/test_tax.py'.",
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "finish",
            "description": "Call this when you are done fixing the bug. Provide a short summary of what you changed and why.",
            "input_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "Summary of the fix."}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_diff",
            "description": (
                "Unified diff of every change made to the workspace so far, relative to "
                "its state before any agent touched it."
            ),
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def _run_list_dir(workspace: Path, args: ListDirArgs) -> dict:
    target = resolve_in_workspace(workspace, args.path)
    if not target.exists():
        raise ToolError(f"no such directory: {args.path}")
    if not target.is_dir():
        raise ToolError(f"not a directory: {args.path}")
    entries = [
        {"name": child.name, "type": "dir" if child.is_dir() else "file"}
        for child in sorted(target.iterdir())
    ]
    return {"path": args.path, "entries": entries}


def _run_read_file(workspace: Path, args: ReadFileArgs) -> dict:
    target = resolve_in_workspace(workspace, args.path)
    if not target.exists() or not target.is_file():
        raise ToolError(f"no such file: {args.path}")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"file is not valid UTF-8 text: {args.path}") from exc
    truncated = len(text) > MAX_READ_CHARS
    if truncated:
        text = text[:MAX_READ_CHARS]
    return {"path": args.path, "content": text, "truncated": truncated}


def _run_search(workspace: Path, args: SearchArgs) -> dict:
    root = resolve_in_workspace(workspace, args.path)
    if not root.exists() or not root.is_dir():
        raise ToolError(f"no such directory: {args.path}")

    workspace_resolved = workspace.resolve()
    matches: list[dict] = []
    for file_path in sorted(root.rglob("*")):
        if len(matches) >= args.max_results:
            break
        if not file_path.is_file():
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = file_path.relative_to(workspace_resolved)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if args.query in line:
                matches.append(
                    {"file": str(rel).replace("\\", "/"), "line": line_number, "text": line.strip()}
                )
                if len(matches) >= args.max_results:
                    break
    return {"query": args.query, "matches": matches, "truncated": len(matches) >= args.max_results}


def _run_edit_file(workspace: Path, args: EditFileArgs) -> dict:
    target = resolve_in_workspace(workspace, args.path)
    if not target.exists() or not target.is_file():
        raise ToolError(f"no such file: {args.path}")
    # newline="" on both read and write: no newline translation, so the file
    # keeps its own line endings (a plain write_text on Windows would turn
    # every LF into CRLF). The model sees "\n" via read_file, so a pure-CRLF
    # file is edited in LF form and converted back. Mixed files are edited raw.
    raw = target.read_text(encoding="utf-8", newline="")
    crlf = "\r\n" in raw and raw.count("\r\n") == raw.count("\n")
    text = raw.replace("\r\n", "\n") if crlf else raw
    old_string, new_string = args.old_string, args.new_string
    if crlf:
        old_string = old_string.replace("\r\n", "\n")
        new_string = new_string.replace("\r\n", "\n")
    count = text.count(old_string)
    if count == 0:
        raise ToolError(f"old_string not found in {args.path}")
    if count > 1:
        raise ToolError(
            f"old_string is not unique in {args.path} ({count} occurrences) -- "
            "include more surrounding context so it matches exactly once"
        )
    new_text = text.replace(old_string, new_string, 1)
    if crlf:
        new_text = new_text.replace("\n", "\r\n")
    target.write_text(new_text, encoding="utf-8", newline="")
    return {"path": args.path, "replaced": 1}


def _run_run_tests(workspace: Path, args: RunTestsArgs) -> dict:
    cmd_target = None
    if args.test_path:
        if args.test_path.startswith("-"):
            raise ToolError("test_path must not look like a command-line flag")
        resolved = resolve_in_workspace(workspace, args.test_path)
        if not resolved.exists():
            raise ToolError(f"no such test path: {args.test_path}")
        cmd_target = args.test_path.replace("\\", "/")

    returncode, output = sandbox.run_pytest(workspace, cmd_target)
    if returncode is None:
        raise ToolError("test run timed out")
    if len(output) > MAX_READ_CHARS:
        output = output[-MAX_READ_CHARS:]
    return {"returncode": returncode, "passed": returncode == 0, "output": output.strip()}


def _run_finish(args: FinishArgs) -> dict:
    return {"summary": args.summary}


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return ["<binary or non-UTF-8 file>\n"]


def workspace_diff(workspace: Path, baseline: Path) -> tuple[list[str], str]:
    """(changed_files, unified diff) of workspace vs `baseline` -- the
    snapshot taken when the workspace was materialized (base + the bug's
    broken files), so the diff shows only agent edits, never the injected bug
    itself. Cache/bytecode paths are skipped exactly as scoring skips them."""
    before = _relative_file_set(baseline)
    after = _relative_file_set(workspace)
    chunks: list[str] = []
    changed: list[str] = []
    for rel in sorted(before | after):
        old = _read_lines(baseline / rel) if rel in before else []
        new = _read_lines(workspace / rel) if rel in after else []
        if old == new:
            continue
        name = str(rel).replace("\\", "/")
        changed.append(name)
        chunks.extend(
            difflib.unified_diff(
                old, new,
                fromfile=f"a/{name}" if rel in before else "/dev/null",
                tofile=f"b/{name}" if rel in after else "/dev/null",
            )
        )
    return changed, "".join(chunks)


def _run_get_diff(workspace: Path, baseline: Path | None) -> dict:
    if baseline is None or not baseline.is_dir():
        raise ToolError("get_diff is unavailable: no baseline snapshot for this workspace")
    changed, diff = workspace_diff(workspace, baseline)
    truncated = len(diff) > MAX_READ_CHARS
    if truncated:
        diff = diff[:MAX_READ_CHARS]
    return {"changed_files": changed, "diff": diff, "truncated": truncated}


def dispatch_tool(
    name: str, raw_args: dict[str, Any], workspace: Path, baseline: Path | None = None
) -> tuple[dict, bool]:
    """Validate and execute a tool call. Returns (result_dict, is_error).

    Callers are expected to log every call -- including rejections -- using
    the returned is_error flag; this function itself never logs.
    """
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return {"error": f"unknown tool: {name}"}, True

    try:
        args = schema.model_validate(raw_args)
    except ValidationError as exc:
        return {"error": f"invalid arguments for {name}: {exc}"}, True

    try:
        if name == "list_dir":
            return _run_list_dir(workspace, args), False
        if name == "read_file":
            return _run_read_file(workspace, args), False
        if name == "search":
            return _run_search(workspace, args), False
        if name == "edit_file":
            return _run_edit_file(workspace, args), False
        if name == "run_tests":
            return _run_run_tests(workspace, args), False
        if name == "finish":
            return _run_finish(args), False
        if name == "get_diff":
            return _run_get_diff(workspace, baseline), False
    except (ToolError, PathEscapeError) as exc:
        return {"error": str(exc)}, True

    return {"error": f"unhandled tool: {name}"}, True
