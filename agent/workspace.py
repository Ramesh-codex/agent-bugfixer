"""Workspace isolation.

Builds a fresh, disposable copy of bugzoo/base with one bug's broken files
overlaid, and validates every path a tool call wants to touch against it.

For list_dir / read_file / search / edit_file this validation is the ONLY
barrier protecting the host filesystem (and the project .env) -- those tools
run directly in this process, not inside the sandbox container. run_tests
gets this same check plus the container's filesystem isolation (only the
workspace directory is bind-mounted in).
"""

import shutil
from pathlib import Path

from agent import config

BASE_DIR = config.ROOT / "bugzoo" / "base"
IGNORE_PATTERNS = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


class PathEscapeError(Exception):
    """Raised when a tool-supplied path would resolve outside the workspace."""


def materialize_workspace(dest: Path, bug_files_dir: Path | None) -> None:
    """Copy base into dest, then overlay a bug's broken files (if any) on top."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(BASE_DIR, dest, ignore=IGNORE_PATTERNS)
    if bug_files_dir is not None and bug_files_dir.exists():
        for src_file in bug_files_dir.rglob("*"):
            if src_file.is_dir():
                continue
            rel = src_file.relative_to(bug_files_dir)
            dest_file = dest / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)


def resolve_in_workspace(workspace: Path, user_path: str | None) -> Path:
    """Resolve a tool-supplied path relative to `workspace`.

    Rejects (raising PathEscapeError):
      - absolute paths, POSIX (`/etc/passwd`) or Windows (`C:\\...`, `\\\\host\\share`)
      - '..' parent-directory traversal anywhere in the path
      - paths that, once resolved (including symlink targets), land outside
        the workspace -- this is the check that catches a symlink planted
        inside the workspace pointing somewhere else on disk
    """
    raw = user_path if user_path not in (None, "") else "."
    normalized = raw.replace("\\", "/")

    if raw.startswith("/") or raw.startswith("\\"):
        raise PathEscapeError(f"absolute paths are not allowed: {raw!r}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise PathEscapeError(f"absolute paths are not allowed: {raw!r}")
    if ".." in normalized.split("/"):
        raise PathEscapeError(f"parent-directory traversal is not allowed: {raw!r}")

    workspace_resolved = workspace.resolve()
    candidate = (workspace_resolved / normalized).resolve()
    try:
        candidate.relative_to(workspace_resolved)
    except ValueError:
        raise PathEscapeError(f"path escapes the workspace: {raw!r}") from None
    return candidate
