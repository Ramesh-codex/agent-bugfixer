"""Milestone 6 (live mode): small, stdlib-only helpers for the two GitHub
Actions workflows in the target repo (agent-propose.yml, agent-approve.yml).

Stdlib only on purpose: the approve job runs these without installing
requirements.txt, and nothing here touches the network or the GitHub API --
the workflows do that with `gh`, after these checks pass.

    python -m agent.gh_live command            # env COMMENT_BODY -> prints approve | reject | none
    python -m agent.gh_live check-actor        # env ACTOR, OWNER -> exit 1 unless the owner
    python -m agent.gh_live find-marker --comments <json> --issue <N>   # key=value lines for $GITHUB_OUTPUT
    python -m agent.gh_live check-patch --patch <file> --sha256 <hex>
    python -m agent.gh_live check-paths --paths-file <file>             # one path per line (git diff --name-only)

Every value that came from GitHub (comment body, comment JSON, patch) is
read from an env var or a file, never from argv built by shell interpolation.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

COMMANDS = {"/approve": "approve", "/reject": "reject"}

MARKER_PREFIX = "<!-- agent-bugfixer-run "
MARKER_SUFFIX = " -->"
_MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + r"(\{[^\n]*?\})" + re.escape(MARKER_SUFFIX))
# Only comments the propose workflow posted (as the Actions bot) can carry a marker.
BOT_LOGIN = "github-actions[bot]"

_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Paths a live patch may never touch. tests/ is the specification (as in the
# bug zoo); the rest could change what the test suite runs or how, or what
# CI does -- each would let a fix "pass" without fixing anything.
PROTECTED_DIRS = {"tests", "test", ".github", ".git"}
PROTECTED_FILES = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "noxfile.py"}
_TEST_MODULE_RE = re.compile(r"^(test_.*|.*_test)\.py$", re.IGNORECASE)

# Patch header lines our own diff (agent.tools.workspace_diff) never emits:
# mode changes (symlinks), renames, binary blobs. Any of them means the patch
# did not come from us unchanged.
_FORBIDDEN_PATCH_LINES = (
    "GIT binary patch", "Binary files ", "old mode ", "new mode ", "new file mode ",
    "deleted file mode ", "rename from ", "rename to ", "copy from ", "copy to ", "similarity index ",
)


# --- commands and actor -------------------------------------------------------

def parse_command(body: str | None) -> str | None:
    """"approve" / "reject" only when the whole comment is exactly the command
    (surrounding whitespace aside); anything else -- including a command
    quoted inside a longer comment, or different casing -- is None."""
    if body is None:
        return None
    return COMMANDS.get(body.strip())


def is_owner(actor: str | None, owner: str | None) -> bool:
    return bool(actor) and bool(owner) and actor == owner


# --- hidden marker --------------------------------------------------------------

def make_marker(*, run_id: str, issue: int, patch_sha256: str | None, base_sha: str | None, verdict: str) -> str:
    data = {"v": 1, "run_id": run_id, "issue": issue, "patch_sha256": patch_sha256,
            "base_sha": base_sha, "verdict": verdict}
    _validate_marker(data)
    return MARKER_PREFIX + json.dumps(data, separators=(",", ":")) + MARKER_SUFFIX


def _validate_marker(data: dict) -> None:
    if data.get("v") != 1:
        raise ValueError("unsupported marker version")
    if not isinstance(data.get("run_id"), str) or not _RUN_ID_RE.match(data["run_id"]):
        raise ValueError("bad run_id")
    if not isinstance(data.get("issue"), int) or isinstance(data["issue"], bool) or data["issue"] < 1:
        raise ValueError("bad issue number")
    sha = data.get("patch_sha256")
    if sha is not None and not (isinstance(sha, str) and _SHA256_RE.match(sha)):
        raise ValueError("bad patch_sha256")
    base = data.get("base_sha")
    if base is not None and not (isinstance(base, str) and _GIT_SHA_RE.match(base)):
        raise ValueError("bad base_sha")
    if data.get("verdict") not in ("needs-approval", "needs-decision"):
        raise ValueError("bad verdict")


def parse_marker(text: str) -> dict | None:
    """The LAST valid marker in `text`, or None."""
    found = None
    for match in _MARKER_RE.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
            _validate_marker(data)
        except (ValueError, TypeError, AttributeError):
            continue
        found = data
    return found


def find_marker(comments: list[dict], issue: int) -> dict | None:
    """Marker from the most recent bot comment on `issue` that has one.
    Comments by anyone else are ignored, so nobody can point /approve at a
    different run by posting a marker of their own."""
    for comment in reversed(comments):
        user = comment.get("user") or {}
        if user.get("login") != BOT_LOGIN or user.get("type") != "Bot":
            continue
        marker = parse_marker(comment.get("body") or "")
        if marker is not None and marker["issue"] == issue:
            return marker
    return None


# --- patch checks ----------------------------------------------------------------

def protected_reason(path: str) -> str | None:
    """Why a patch may not touch `path`, or None if it may."""
    p = path.strip().replace("\\", "/")
    if not p:
        return "empty path"
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return f"absolute path: {path}"
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if ".." in parts:
        return f"parent-directory traversal: {path}"
    lowered = [x.lower() for x in parts]
    for part in lowered[:-1]:
        if part in PROTECTED_DIRS:
            return f"{path} is under {part}/ (tests and CI config are never changed)"
    name = lowered[-1] if lowered else ""
    if name in PROTECTED_FILES:
        return f"{path} is test/build configuration"
    if _TEST_MODULE_RE.match(name):
        return f"{path} is a test module"
    return None


def _header_path(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            path = _header_path(line[4:])
            if path is not None and path not in paths:
                paths.append(path)
        elif line.startswith("diff --git "):
            for raw in line[len("diff --git "):].split(" "):
                path = _header_path(raw)
                if path and path not in paths:
                    paths.append(path)
    return paths


def check_patch(patch: str) -> list[str]:
    """Problems that make `patch` unacceptable; empty list = OK."""
    if not patch.strip():
        return ["patch is empty"]
    problems = []
    for line in patch.splitlines():
        if line.startswith(_FORBIDDEN_PATCH_LINES):
            problems.append(f"unsupported patch line: {line[:80]}")
    paths = patch_paths(patch)
    if not paths:
        problems.append("patch names no files")
    problems += [r for r in map(protected_reason, paths) if r]
    return problems


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- CLI ---------------------------------------------------------------------------

def _fail(msg: str) -> int:
    print(f"refused: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks used by the agent-bugfixer GitHub workflows.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("command", help="parse env COMMENT_BODY")
    sub.add_parser("check-actor", help="env ACTOR must equal env OWNER")
    p = sub.add_parser("find-marker")
    p.add_argument("--comments", required=True, type=Path)
    p.add_argument("--issue", required=True, type=int)
    p = sub.add_parser("check-patch")
    p.add_argument("--patch", required=True, type=Path)
    p.add_argument("--sha256", required=True)
    p = sub.add_parser("check-paths")
    p.add_argument("--paths-file", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "command":
        print(parse_command(os.environ.get("COMMENT_BODY")) or "none")
        return 0

    if args.cmd == "check-actor":
        if not is_owner(os.environ.get("ACTOR"), os.environ.get("OWNER")):
            return _fail("actor is not the repository owner")
        return 0

    if args.cmd == "find-marker":
        comments = json.loads(args.comments.read_text(encoding="utf-8"))
        # `gh api --paginate --slurp` yields a list of pages; plain `gh api` a list of comments.
        if comments and isinstance(comments[0], list):
            comments = [c for page in comments for c in page]
        marker = find_marker(comments, args.issue)
        if marker is None:
            return _fail(f"no agent-bugfixer run marker from {BOT_LOGIN} on issue #{args.issue}")
        print(f"run_id={marker['run_id']}")
        print(f"patch_sha256={marker['patch_sha256'] or ''}")
        print(f"base_sha={marker['base_sha'] or ''}")
        print(f"verdict={marker['verdict']}")
        return 0

    if args.cmd == "check-patch":
        data = args.patch.read_bytes()
        if not _SHA256_RE.match(args.sha256) or sha256_hex(data) != args.sha256:
            return _fail("patch checksum does not match the run marker")
        problems = check_patch(data.decode("utf-8"))
        if problems:
            return _fail("; ".join(problems))
        print("patch OK: " + ", ".join(patch_paths(data.decode("utf-8"))))
        return 0

    if args.cmd == "check-paths":
        paths = [line for line in args.paths_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not paths:
            return _fail("no changed paths")
        problems = [r for r in map(protected_reason, paths) if r]
        if problems:
            return _fail("; ".join(problems))
        print("paths OK: " + ", ".join(paths))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
