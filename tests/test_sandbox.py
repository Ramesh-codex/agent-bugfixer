"""to_mount_path: Docker Desktop //drive/ form on Windows, plain path elsewhere (CI)."""

from pathlib import PureWindowsPath

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
