"""to_mount_path: Docker Desktop //drive/ form on Windows, plain path elsewhere (CI)."""

from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from agent import sandbox


def test_non_windows_mount_path_is_the_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert sandbox.to_mount_path(tmp_path) == str(tmp_path.resolve())


def test_windows_mount_path_uses_docker_desktop_drive_form(monkeypatch):
    class FakePath:
        def resolve(self):
            return PureWindowsPath("D:\\work\\ws")

    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    assert sandbox.to_mount_path(FakePath()) == "//d/work/ws"


# --- container user + no cache files (FINDINGS.md F12) -------------------------

import scripts.verify_zoo as verify_zoo  # noqa: E402


def _posix(monkeypatch, module):
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "getuid", lambda: 1001, raising=False)
    monkeypatch.setattr(module.os, "getgid", lambda: 118, raising=False)


def _flag_value(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_posix_container_runs_as_host_user(tmp_path, monkeypatch):
    _posix(monkeypatch, sandbox)
    cmd = sandbox.pytest_command(tmp_path)
    assert _flag_value(cmd, "--user") == "1001:118"
    # --user is a docker option, so it must come before the image name.
    assert cmd.index("--user") < cmd.index(sandbox.config.SANDBOX_IMAGE)


def test_windows_container_has_no_user_flag(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    assert sandbox.container_user_args() == []


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_no_cache_files_on_every_platform(tmp_path, monkeypatch, platform):
    if platform == "linux":
        _posix(monkeypatch, sandbox)
    else:
        monkeypatch.setattr(sandbox.sys, "platform", "win32")
        monkeypatch.setattr(sandbox, "to_mount_path", lambda p: "//d/ws")
    cmd = sandbox.pytest_command(tmp_path, "tests/test_tax.py")
    image = cmd.index(sandbox.config.SANDBOX_IMAGE)
    assert _flag_value(cmd, "-e") == "PYTHONDONTWRITEBYTECODE=1" and cmd.index("-e") < image
    assert cmd[cmd.index("-p") + 1] == "no:cacheprovider" and cmd.index("-p") > image
    assert cmd[-1] == "tests/test_tax.py"
    assert _flag_value(cmd, "--network") == "none"


def test_run_pytest_uses_the_built_command(tmp_path, monkeypatch):
    _posix(monkeypatch, sandbox)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="1 passed", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.run_pytest(tmp_path)[0] == 0
    assert seen["cmd"] == sandbox.pytest_command(tmp_path)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_verify_zoo_uses_the_same_sandbox_flags(tmp_path, monkeypatch, platform):
    """verify_zoo.py stays standalone (no requirements in its CI job), so it
    duplicates the docker command -- this keeps the two in step."""
    for module in (sandbox, verify_zoo):
        if platform == "linux":
            _posix(monkeypatch, module)
        else:
            monkeypatch.setattr(module.sys, "platform", "win32")
            monkeypatch.setattr(module, "to_mount_path", lambda p: "//d/ws")
    assert verify_zoo.suite_command(tmp_path) == sandbox.pytest_command(tmp_path)
