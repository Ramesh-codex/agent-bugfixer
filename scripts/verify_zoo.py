"""Verify every bug in bugzoo/bugs/ against the clean base library.

For each bug: overlay its broken files onto a fresh copy of bugzoo/base and
run the base pytest suite inside the `bugzoo-sandbox` container (no network,
512m memory, 1 cpu, 60s timeout). Every bug must make the suite fail. The
clean base, run the same way with no overlay, must pass.

Writes a results table to stdout and a machine-readable report to
reports/verify_zoo.json.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = ROOT / "bugzoo" / "base"
BUGS_DIR = ROOT / "bugzoo" / "bugs"
TMP_DIR = ROOT / "reports" / "tmp"
REPORT_PATH = ROOT / "reports" / "verify_zoo.json"

IMAGE = "bugzoo-sandbox"
CONTAINER_TIMEOUT_S = 55  # enforced inside the container via coreutils `timeout`
SUBPROCESS_TIMEOUT_S = 75  # outer watchdog, must exceed CONTAINER_TIMEOUT_S
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1"

IGNORE_PATTERNS = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


@dataclass
class RunResult:
    id: str
    category: str
    difficulty: str
    expected: str  # "pass" or "fail"
    returncode: int | None
    passed_suite: bool
    summary: str
    duration_s: float
    ok: bool  # did the outcome match `expected`?
    detail: str = ""


def materialize(dest: Path, overlay_files_dir: Path | None) -> None:
    """Copy base into dest, then overlay broken files (if any) on top."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(BASE_DIR, dest, ignore=IGNORE_PATTERNS)
    if overlay_files_dir is not None:
        for src_file in overlay_files_dir.rglob("*"):
            if src_file.is_dir():
                continue
            rel = src_file.relative_to(overlay_files_dir)
            dest_file = dest / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)


def to_mount_path(host_path: Path) -> str:
    """Host path in the form `docker run -v` expects: //drive/... for Docker
    Desktop on Windows, the plain absolute path on Linux/macOS (e.g. CI)."""
    resolved = str(host_path.resolve())
    if sys.platform != "win32":
        return resolved
    drive, rest = resolved.split(":", 1)
    rest = rest.replace("\\", "/")
    return f"//{drive.lower()}{rest}"


def container_user_args() -> list[str]:
    """Same as agent/sandbox.py (kept standalone: this script needs no
    requirements). On Linux/macOS the container runs as the host user, so
    nothing it writes into the mount is root-owned (FINDINGS.md F12)."""
    if sys.platform == "win32":
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def suite_command(workdir: Path) -> list[str]:
    return [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", MEMORY_LIMIT,
        "--cpus", CPU_LIMIT,
        *container_user_args(),
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{to_mount_path(workdir)}:/workspace",
        "-w", "/workspace",
        IMAGE,
        "timeout", str(CONTAINER_TIMEOUT_S),
        "python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
    ]


def run_suite(workdir: Path) -> tuple[int | None, str, float, str]:
    cmd = suite_command(workdir)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S
        )
        duration = time.monotonic() - start
        return proc.returncode, proc.stdout + "\n" + proc.stderr, duration, ""
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return None, output, duration, "subprocess watchdog timeout exceeded"


def summarize(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip()
    return lines[-1].strip() if lines else "(no output)"


def check_one(
    label: str,
    category: str,
    difficulty: str,
    expected: str,
    overlay_files_dir: Path | None,
) -> RunResult:
    workdir = TMP_DIR / label
    materialize(workdir, overlay_files_dir)
    returncode, output, duration, err = run_suite(workdir)
    summary = summarize(output)

    if err:
        ok = False
        detail = err
    elif expected == "pass":
        ok = returncode == 0
        detail = "" if ok else f"expected clean base to PASS but returncode={returncode}: {summary}"
    else:
        ok = returncode is not None and returncode != 0
        detail = "" if ok else "expected bug to make a test FAIL but the suite PASSED"

    return RunResult(
        id=label,
        category=category,
        difficulty=difficulty,
        expected=expected,
        returncode=returncode,
        passed_suite=(returncode == 0),
        summary=summary,
        duration_s=round(duration, 2),
        ok=ok,
        detail=detail,
    )


def print_table(results: list[RunResult]) -> None:
    headers = ["ID", "Category", "Difficulty", "Expected", "Result", "Summary", "Time(s)", "Check"]
    rows = []
    for r in results:
        result_str = "PASS" if r.passed_suite else ("TIMEOUT" if r.returncode is None else "FAIL")
        rows.append(
            [
                r.id,
                r.category,
                r.difficulty,
                r.expected.upper(),
                result_str,
                r.summary[:42],
                f"{r.duration_s:.2f}",
                "OK" if r.ok else "MISMATCH",
            ]
        )

    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]

    def fmt_row(cols: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cols, widths))

    print()
    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def main() -> int:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    results: list[RunResult] = []

    print("Checking clean base...")
    results.append(check_one("BASE", "-", "-", "pass", None))

    bug_dirs = sorted(p for p in BUGS_DIR.iterdir() if p.is_dir() and p.name.startswith("BUG-"))
    for bug_dir in bug_dirs:
        meta = json.loads((bug_dir / "meta.json").read_text(encoding="utf-8"))
        print(f"Checking {meta['id']}...")
        results.append(
            check_one(meta["id"], meta["category"], meta["difficulty"], "fail", bug_dir / "files")
        )

    print_table(results)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"\nWrote {REPORT_PATH}")

    failures = [r for r in results if not r.ok]
    if failures:
        print(f"\n{len(failures)} INTEGRITY FAILURE(S):")
        for r in failures:
            print(f"  {r.id}: {r.detail}")
        return 1

    print(f"\nAll {len(results)} checks OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
