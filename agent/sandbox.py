"""Shared helper for invoking pytest inside the bugzoo-sandbox container."""

import os
import subprocess
import sys
from pathlib import Path

from agent import config


def to_mount_path(host_path: Path) -> str:
    """Host path in the form `docker run -v` expects: //drive/... for Docker
    Desktop on Windows, the plain absolute path on Linux/macOS (e.g. CI)."""
    resolved = str(host_path.resolve())
    if sys.platform != "win32":
        return resolved
    drive, rest = resolved.split(":", 1)
    rest = rest.replace("\\", "/")
    return f"//{drive.lower()}{rest}"


def warm_up() -> None:
    """Run one throwaway container so the first timed run isn't skewed by
    image-load / cold-start overhead."""
    subprocess.run(
        ["docker", "run", "--rm", config.SANDBOX_IMAGE, "python", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def container_user_args() -> list[str]:
    """`--user <uid>:<gid>` of the host user on Linux/macOS, so anything the
    container writes into the bind-mounted workspace is owned by that user,
    not root (FINDINGS.md F12). Docker Desktop on Windows maps ownership
    itself and the host has no uid, so nothing there."""
    if sys.platform == "win32":
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


# No bytecode, no .pytest_cache: the container should leave nothing behind in
# the workspace except what the code under test itself writes.
NO_CACHE_ENV = ["-e", "PYTHONDONTWRITEBYTECODE=1"]
PYTEST_ARGS = ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]


def pytest_command(workspace: Path, test_target: str | None = None) -> list[str]:
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", config.SANDBOX_MEMORY,
        "--cpus", config.SANDBOX_CPUS,
        *container_user_args(),
        *NO_CACHE_ENV,
        "-v", f"{to_mount_path(workspace)}:/workspace",
        "-w", "/workspace",
        config.SANDBOX_IMAGE,
        "timeout", str(config.CONTAINER_TEST_TIMEOUT_S),
        *PYTEST_ARGS,
    ]
    if test_target:
        cmd.append(test_target)
    return cmd


def run_pytest(workspace: Path, test_target: str | None = None) -> tuple[int | None, str]:
    """Run pytest against `workspace` inside the sandbox.

    Returns (returncode, combined_output). returncode is None on a timeout.
    """
    cmd = pytest_command(workspace, test_target)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.SUBPROCESS_TEST_TIMEOUT_S
        )
        return proc.returncode, proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return None, output
