"""Shared helper for invoking pytest inside the bugzoo-sandbox container."""

import subprocess
from pathlib import Path

from agent import config


def to_mount_path(win_path: Path) -> str:
    """Convert a Windows path to the //drive/... form Docker Desktop expects."""
    resolved = str(win_path.resolve())
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


def run_pytest(workspace: Path, test_target: str | None = None) -> tuple[int | None, str]:
    """Run pytest against `workspace` inside the sandbox.

    Returns (returncode, combined_output). returncode is None on a timeout.
    """
    mount = to_mount_path(workspace)
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", config.SANDBOX_MEMORY,
        "--cpus", config.SANDBOX_CPUS,
        "-v", f"{mount}:/workspace",
        "-w", "/workspace",
        config.SANDBOX_IMAGE,
        "timeout", str(config.CONTAINER_TEST_TIMEOUT_S),
        "python", "-m", "pytest", "-q", "--tb=short",
    ]
    if test_target:
        cmd.append(test_target)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.SUBPROCESS_TEST_TIMEOUT_S
        )
        return proc.returncode, proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return None, output
