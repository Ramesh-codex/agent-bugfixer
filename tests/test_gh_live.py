"""Offline tests for live mode's workflow helpers (agent/gh_live.py) and a
static scan of the two workflow files. No network, no API calls."""

import json
import re
from pathlib import Path

import pytest

from agent import gh_live

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "live" / "workflows"
WORKFLOWS = {name: TEMPLATES / name for name in ("agent-propose.yml", "agent-approve.yml")}
# The bugzoo-target clone next to this repo, if present: its workflow files
# must be these templates (except for the pinned agent-bugfixer commit).
TARGET_WORKFLOWS = ROOT.parent / "bugzoo-target" / ".github" / "workflows"

SHA = "a" * 64
BASE = "b" * 40


# --- command parsing -------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ("/approve", "approve"),
    ("/reject", "reject"),
    ("  /approve\n", "approve"),
    ("/approve please", None),
    ("I /approve", None),
    ("> /approve\n\nquoted", None),
    ("/APPROVE", None),
    ("/approved", None),
    ("", None),
    (None, None),
])
def test_parse_command(body, expected):
    assert gh_live.parse_command(body) == expected


def test_command_cli_reads_env_only(monkeypatch, capsys):
    monkeypatch.setenv("COMMENT_BODY", "/reject")
    assert gh_live.main(["command"]) == 0
    assert capsys.readouterr().out.strip() == "reject"
    monkeypatch.setenv("COMMENT_BODY", "$(curl evil) /approve")
    gh_live.main(["command"])
    assert capsys.readouterr().out.strip() == "none"


# --- actor check -------------------------------------------------------------

@pytest.mark.parametrize("actor,owner,ok", [
    ("Ramesh-codex", "Ramesh-codex", True),
    ("someone-else", "Ramesh-codex", False),
    ("github-actions[bot]", "Ramesh-codex", False),
    ("", "", False),
    (None, "Ramesh-codex", False),
])
def test_is_owner(actor, owner, ok):
    assert gh_live.is_owner(actor, owner) is ok


def test_check_actor_cli(monkeypatch):
    monkeypatch.setenv("ACTOR", "intruder")
    monkeypatch.setenv("OWNER", "Ramesh-codex")
    assert gh_live.main(["check-actor"]) == 1
    monkeypatch.setenv("ACTOR", "Ramesh-codex")
    assert gh_live.main(["check-actor"]) == 0


# --- marker -------------------------------------------------------------------

def _bot(body):
    return {"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}


def test_marker_round_trip():
    marker = gh_live.make_marker(run_id="123456", issue=7, patch_sha256=SHA, base_sha=BASE, verdict="needs-approval")
    assert marker.startswith("<!--") and marker.endswith("-->")
    parsed = gh_live.parse_marker("some comment\n\n" + marker + "\n")
    assert parsed == {"v": 1, "run_id": "123456", "issue": 7, "patch_sha256": SHA, "base_sha": BASE,
                      "verdict": "needs-approval"}


def test_marker_without_patch_round_trips():
    marker = gh_live.make_marker(run_id="1", issue=1, patch_sha256=None, base_sha=None, verdict="needs-decision")
    assert gh_live.parse_marker(marker)["patch_sha256"] is None


@pytest.mark.parametrize("kwargs", [
    dict(run_id="1; rm -rf /", issue=1, patch_sha256=None, base_sha=None, verdict="needs-decision"),
    dict(run_id="1", issue=0, patch_sha256=None, base_sha=None, verdict="needs-decision"),
    dict(run_id="1", issue=1, patch_sha256="xyz", base_sha=None, verdict="needs-decision"),
    dict(run_id="1", issue=1, patch_sha256=None, base_sha="main", verdict="needs-decision"),
    dict(run_id="1", issue=1, patch_sha256=None, base_sha=None, verdict="approved"),
])
def test_make_marker_refuses_bad_values(kwargs):
    with pytest.raises(ValueError):
        gh_live.make_marker(**kwargs)


def test_parse_marker_skips_tampered_json():
    bad = '<!-- agent-bugfixer-run {"v":1,"run_id":"$(x)","issue":1,"patch_sha256":null,"base_sha":null,"verdict":"needs-decision"} -->'
    assert gh_live.parse_marker(bad) is None


def test_find_marker_uses_latest_bot_comment_for_this_issue():
    old = gh_live.make_marker(run_id="1", issue=5, patch_sha256=SHA, base_sha=None, verdict="needs-approval")
    new = gh_live.make_marker(run_id="2", issue=5, patch_sha256=SHA, base_sha=None, verdict="needs-approval")
    other_issue = gh_live.make_marker(run_id="3", issue=6, patch_sha256=SHA, base_sha=None, verdict="needs-approval")
    forged = gh_live.make_marker(run_id="666", issue=5, patch_sha256=SHA, base_sha=None, verdict="needs-approval")
    comments = [
        _bot(old), _bot(new), _bot(other_issue),
        {"user": {"login": "Ramesh-codex", "type": "User"}, "body": forged},
        {"user": {"login": "github-actions[bot]", "type": "User"}, "body": forged},  # not really the bot
    ]
    assert gh_live.find_marker(comments, 5)["run_id"] == "2"
    assert gh_live.find_marker(comments, 9) is None


def test_find_marker_cli_handles_slurped_pages(tmp_path, capsys):
    marker = gh_live.make_marker(run_id="42", issue=3, patch_sha256=SHA, base_sha=BASE, verdict="needs-approval")
    path = tmp_path / "comments.json"
    path.write_text(json.dumps([[_bot("hi")], [_bot("result\n" + marker)]]), encoding="utf-8")
    assert gh_live.main(["find-marker", "--comments", str(path), "--issue", "3"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["run_id=42", f"patch_sha256={SHA}", f"base_sha={BASE}", "verdict=needs-approval"]


# --- patch checks ----------------------------------------------------------------

LIB_PATCH = (
    "--- a/library/discounts.py\n+++ b/library/discounts.py\n@@ -1,2 +1,2 @@\n"
    "-    if subtotal > 50:\n+    if subtotal >= 50:\n"
)
TESTS_PATCH = (
    "--- a/tests/test_discounts.py\n+++ b/tests/test_discounts.py\n@@ -1 +1 @@\n"
    "-    assert x == 1\n+    assert True\n"
)


def test_library_patch_is_accepted():
    assert gh_live.check_patch(LIB_PATCH) == []
    assert gh_live.patch_paths(LIB_PATCH) == ["library/discounts.py"]


@pytest.mark.parametrize("path", [
    "tests/test_discounts.py", "Tests/test_x.py", "./tests/x.py", "pkg/tests/helpers.py",
    "conftest.py", "library/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
    ".github/workflows/agent-approve.yml", "library/test_utils.py", "library/utils_test.py",
    "../outside.py", "/etc/passwd", "C:/x.py",
])
def test_protected_paths_are_refused(path):
    assert gh_live.protected_reason(path) is not None


@pytest.mark.parametrize("path", ["library/discounts.py", "library/testing_helpers.py", "README.md"])
def test_ordinary_paths_are_allowed(path):
    assert gh_live.protected_reason(path) is None


def test_patch_touching_tests_is_refused():
    problems = gh_live.check_patch(LIB_PATCH + TESTS_PATCH)
    assert problems and all("tests/" in p for p in problems)


def test_patch_with_git_header_into_tests_is_refused():
    patch = "diff --git a/library/x.py b/tests/x.py\nrename from library/x.py\nrename to tests/x.py\n"
    problems = gh_live.check_patch(patch)
    assert any("tests/" in p for p in problems)
    assert any("unsupported patch line" in p for p in problems)


@pytest.mark.parametrize("line", ["new file mode 120000", "GIT binary patch", "old mode 100644"])
def test_patch_metadata_lines_are_refused(line):
    assert gh_live.check_patch(LIB_PATCH + line + "\n")


def test_empty_patch_is_refused():
    assert gh_live.check_patch("  \n") == ["patch is empty"]


def test_check_patch_cli_verifies_checksum_and_paths(tmp_path):
    good = tmp_path / "good.patch"
    good.write_bytes(LIB_PATCH.encode())
    assert gh_live.main(["check-patch", "--patch", str(good), "--sha256", gh_live.sha256_hex(LIB_PATCH.encode())]) == 0
    assert gh_live.main(["check-patch", "--patch", str(good), "--sha256", SHA]) == 1

    bad = tmp_path / "bad.patch"
    bad.write_bytes(TESTS_PATCH.encode())
    assert gh_live.main(["check-patch", "--patch", str(bad), "--sha256", gh_live.sha256_hex(TESTS_PATCH.encode())]) == 1


def test_check_paths_cli(tmp_path):
    paths = tmp_path / "paths.txt"
    paths.write_text("library/discounts.py\n", encoding="utf-8")
    assert gh_live.main(["check-paths", "--paths-file", str(paths)]) == 0
    paths.write_text("library/discounts.py\ntests/test_discounts.py\n", encoding="utf-8")
    assert gh_live.main(["check-paths", "--paths-file", str(paths)]) == 1
    paths.write_text("", encoding="utf-8")
    assert gh_live.main(["check-paths", "--paths-file", str(paths)]) == 1


# --- workflow YAML scan ---------------------------------------------------------

_RUN_RE = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")


def run_blocks(text: str) -> list[str]:
    """Every `run:` script in a workflow file: the inline value, or the
    indented block under `run: |`."""
    lines = text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        m = _RUN_RE.match(line)
        if not m:
            continue
        indent, value = len(m.group(1)), m.group(2)
        body = [value]
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        blocks.append("\n".join(body))
    return blocks


def jobs(text: str) -> dict[str, str]:
    """Job name -> its YAML text (jobs are the 2-space-indented keys under `jobs:`)."""
    section = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", section, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


@pytest.fixture(params=sorted(WORKFLOWS))
def workflow(request):
    return request.param, WORKFLOWS[request.param].read_text(encoding="utf-8")


def test_run_block_scanner_finds_interpolation():
    sample = "steps:\n  - run: echo ${{ github.event.issue.title }}\n  - name: x\n    run: |\n      a\n      echo \"${{ github.event.comment.body }}\"\n    env:\n      X: ${{ github.event.issue.body }}\n"
    blocks = run_blocks(sample)
    assert len(blocks) == 2
    assert all("github.event" in b for b in blocks)
    assert "issue.body" not in "".join(blocks)  # env values are not part of run:


def test_no_expression_in_any_run_line(workflow):
    name, text = workflow
    blocks = run_blocks(text)
    assert blocks, name
    for block in blocks:
        assert "github.event" not in block, f"{name}: github.event text in a run: line"
        assert "${{" not in block, f"{name}: expression interpolated into a run: line"


def test_workflow_parses_and_no_run_step_interpolates(workflow):
    """Same rule as above, on the parsed YAML -- also catches files that are
    not valid YAML at all. PyYAML comes in transitively via langgraph."""
    yaml = pytest.importorskip("yaml")
    name, text = workflow
    data = yaml.safe_load(text)
    steps = [s for job in data["jobs"].values() for s in job["steps"]]
    runs = [s["run"] for s in steps if "run" in s]
    assert runs
    for script in runs:
        assert "${{" not in script, f"{name}: {script!r}"
    for job in data["jobs"].values():
        assert job["timeout-minutes"] > 0 and job["permissions"]


def test_triggers_are_only_issues_and_issue_comment(workflow):
    name, text = workflow
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "pull_request_target" not in code
    on = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    triggers = re.findall(r"^  ([\w-]+):", on, flags=re.MULTILINE)
    expected = {"agent-propose.yml": ["issues"], "agent-approve.yml": ["issue_comment"]}[name]
    assert triggers == expected
    assert re.search(r"types: \[(labeled|created)\]", on)


def test_every_job_is_owner_only_least_privilege_with_timeout(workflow):
    name, text = workflow
    assert "\npermissions: {}\n" in text  # workflow default: no token scopes
    assert "concurrency:\n  group: agent-issue-${{ github.event.issue.number }}" in text
    expected = {
        "propose": {"contents: read", "issues: write"},
        "approve": {"contents: write", "pull-requests: write", "issues: write", "actions: read"},
        "reject": {"issues: write"},
    }
    found = jobs(text)
    assert found
    for job, body in found.items():
        assert "github.actor == github.repository_owner" in body, job
        assert "!github.event.repository.fork" in body, job
        assert re.search(r"^    timeout-minutes: \d+$", body, flags=re.MULTILINE), job
        perms = body.split("    permissions:\n", 1)[1].split("    steps:", 1)[0]
        assert {p.strip() for p in perms.strip().splitlines()} == expected[job], job
    if name == "agent-approve.yml":
        assert "github.event.comment.body == '/approve'" in found["approve"]
        assert "github.event.comment.body == '/reject'" in found["reject"]
        assert "github.event.issue.pull_request == null" in found["approve"]
    else:
        assert "github.event.label.name == 'agent-fix'" in found["propose"]


def test_actions_are_pinned_to_full_shas(workflow):
    name, text = workflow
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{name}: {ref} is not pinned to a commit SHA"


def test_agent_bugfixer_checked_out_at_a_pinned_ref(workflow):
    name, text = workflow
    ref = re.search(r"^  AGENT_BUGFIXER_REF: (\S+)$", text, flags=re.MULTILINE).group(1)
    assert ref == "REPLACE_WITH_AGENT_BUGFIXER_COMMIT_SHA" or re.fullmatch(r"[0-9a-f]{40}", ref)
    assert text.count("ref: ${{ env.AGENT_BUGFIXER_REF }}") == text.count("repository: ${{ env.AGENT_BUGFIXER_REPO }}")


def test_api_key_only_in_the_propose_agent_step(workflow):
    name, text = workflow
    secrets = re.findall(r"secrets\.\w+", text)
    if name == "agent-approve.yml":
        assert secrets == []
    else:
        assert secrets == ["secrets.ANTHROPIC_API_KEY"]
        step = text.split("- name: Run the multi-agent pipeline", 1)[1].split("\n      - ", 1)[0]
        assert "secrets.ANTHROPIC_API_KEY" in step


def test_sandbox_keeps_network_none():
    assert '"--network", "none"' in (ROOT / "agent" / "sandbox.py").read_text(encoding="utf-8")


@pytest.mark.skipif(not TARGET_WORKFLOWS.is_dir(), reason="no bugzoo-target clone next to this repo")
@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_target_repo_copies_match_the_templates(name):
    def without_ref(text):
        return re.sub(r"^  AGENT_BUGFIXER_REF: .*$", "", text, flags=re.MULTILINE)

    copy = TARGET_WORKFLOWS / name
    assert copy.exists(), f"{copy} missing"
    assert without_ref(copy.read_text(encoding="utf-8")) == without_ref(WORKFLOWS[name].read_text(encoding="utf-8"))
