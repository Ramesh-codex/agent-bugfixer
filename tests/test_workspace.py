import pytest

from agent.workspace import PathEscapeError, resolve_in_workspace


def test_plain_relative_path_resolves_inside_workspace(tmp_path):
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "tax.py").write_text("x = 1", encoding="utf-8")
    resolved = resolve_in_workspace(tmp_path, "library/tax.py")
    assert resolved == (tmp_path / "library" / "tax.py").resolve()


def test_dot_and_none_resolve_to_workspace_root(tmp_path):
    assert resolve_in_workspace(tmp_path, ".") == tmp_path.resolve()
    assert resolve_in_workspace(tmp_path, None) == tmp_path.resolve()
    assert resolve_in_workspace(tmp_path, "") == tmp_path.resolve()


@pytest.mark.parametrize(
    "bad_path",
    ["../outside.txt", "library/../../outside.txt", ".."],
)
def test_parent_traversal_is_rejected(tmp_path, bad_path):
    with pytest.raises(PathEscapeError):
        resolve_in_workspace(tmp_path, bad_path)


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "\\windows\\system32",
        "C:/Windows/System32",
        "C:\\Windows\\System32",
        "D:\\Projects\\agent-bugfixer\\.env",
    ],
)
def test_absolute_paths_are_rejected(tmp_path, bad_path):
    with pytest.raises(PathEscapeError):
        resolve_in_workspace(tmp_path, bad_path)


def test_symlink_escaping_workspace_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside_target_for_symlink_test"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(PathEscapeError):
        resolve_in_workspace(workspace, "escape/secret.txt")
