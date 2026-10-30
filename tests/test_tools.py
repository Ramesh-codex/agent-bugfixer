import pytest
from pydantic import ValidationError

from agent import tools


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "library").mkdir()
    (ws / "library" / "tax.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (ws / "tests").mkdir()
    (ws / "tests" / "test_tax.py").write_text(
        "from library.tax import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return ws


# --- schema validation (invalid args rejected) --------------------------

def test_list_dir_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        tools.ListDirArgs.model_validate({"path": ".", "unexpected": "x"})


def test_read_file_requires_path():
    with pytest.raises(ValidationError):
        tools.ReadFileArgs.model_validate({})


def test_search_rejects_out_of_range_max_results():
    with pytest.raises(ValidationError):
        tools.SearchArgs.model_validate({"query": "x", "max_results": 0})


def test_search_rejects_empty_query():
    with pytest.raises(ValidationError):
        tools.SearchArgs.model_validate({"query": ""})


def test_finish_rejects_missing_summary():
    with pytest.raises(ValidationError):
        tools.FinishArgs.model_validate({})


def test_dispatch_rejects_invalid_args(workspace):
    result, is_error = tools.dispatch_tool("read_file", {}, workspace)
    assert is_error is True
    assert "error" in result


def test_dispatch_rejects_unknown_tool(workspace):
    result, is_error = tools.dispatch_tool("delete_everything", {}, workspace)
    assert is_error is True


# --- path traversal rejected, end to end via dispatch_tool ---------------

def test_dispatch_rejects_path_traversal(workspace):
    result, is_error = tools.dispatch_tool(
        "read_file", {"path": "../../../etc/passwd"}, workspace
    )
    assert is_error is True


def test_dispatch_rejects_absolute_path(workspace):
    result, is_error = tools.dispatch_tool(
        "read_file", {"path": "C:/Windows/System32/drivers/etc/hosts"}, workspace
    )
    assert is_error is True


def test_dispatch_rejects_env_file_access_outside_workspace(workspace):
    result, is_error = tools.dispatch_tool(
        "read_file", {"path": "../.env"}, workspace
    )
    assert is_error is True


# --- happy paths -----------------------------------------------------------

def test_read_file_returns_contents(workspace):
    result, is_error = tools.dispatch_tool("read_file", {"path": "library/tax.py"}, workspace)
    assert is_error is False
    assert "def add" in result["content"]


def test_list_dir_lists_entries(workspace):
    result, is_error = tools.dispatch_tool("list_dir", {"path": "library"}, workspace)
    assert is_error is False
    names = {e["name"] for e in result["entries"]}
    assert "tax.py" in names


def test_search_finds_literal_substring(workspace):
    result, is_error = tools.dispatch_tool("search", {"query": "return a + b"}, workspace)
    assert is_error is False
    assert len(result["matches"]) == 1
    assert result["matches"][0]["file"] == "library/tax.py"


def test_search_does_not_treat_query_as_regex(workspace):
    # "." as a regex would match any character and hit every line. As a plain
    # substring it must only match lines containing a literal period.
    (workspace / "library" / "weird.py").write_text(
        "has_dot = 'a.b'\nno_dot = 'ab'\n", encoding="utf-8"
    )
    result, is_error = tools.dispatch_tool("search", {"query": "."}, workspace)
    assert is_error is False
    matched_texts = {m["text"] for m in result["matches"]}
    assert "has_dot = 'a.b'" in matched_texts
    assert "no_dot = 'ab'" not in matched_texts


def test_finish_tool_returns_summary(workspace):
    result, is_error = tools.dispatch_tool("finish", {"summary": "fixed it"}, workspace)
    assert is_error is False
    assert result["summary"] == "fixed it"


# --- edit_file uniqueness --------------------------------------------------

def test_edit_file_rejects_non_unique_match(workspace):
    (workspace / "library" / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    result, is_error = tools.dispatch_tool(
        "edit_file",
        {"path": "library/dup.py", "old_string": "x = 1", "new_string": "x = 2"},
        workspace,
    )
    assert is_error is True
    assert "unique" in result["error"]
    # file must be untouched
    assert (workspace / "library" / "dup.py").read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_edit_file_rejects_missing_old_string(workspace):
    result, is_error = tools.dispatch_tool(
        "edit_file",
        {"path": "library/tax.py", "old_string": "not present anywhere", "new_string": "x"},
        workspace,
    )
    assert is_error is True


def test_edit_file_applies_unique_replacement(workspace):
    result, is_error = tools.dispatch_tool(
        "edit_file",
        {"path": "library/tax.py", "old_string": "return a + b", "new_string": "return a - b"},
        workspace,
    )
    assert is_error is False
    content = (workspace / "library" / "tax.py").read_text(encoding="utf-8")
    assert content.count("return a - b") == 1
    assert "return a + b" not in content


def test_edit_file_rejects_path_traversal(workspace):
    result, is_error = tools.dispatch_tool(
        "edit_file",
        {"path": "../../outside.py", "old_string": "a", "new_string": "b"},
        workspace,
    )
    assert is_error is True


# --- run_tests argument validation (no docker invocation on these paths) --

def test_run_tests_rejects_flag_like_test_path(workspace):
    result, is_error = tools.dispatch_tool("run_tests", {"test_path": "-x"}, workspace)
    assert is_error is True


def test_run_tests_rejects_traversal_test_path(workspace):
    result, is_error = tools.dispatch_tool("run_tests", {"test_path": "../../etc"}, workspace)
    assert is_error is True


def test_run_tests_rejects_nonexistent_test_path(workspace):
    result, is_error = tools.dispatch_tool(
        "run_tests", {"test_path": "tests/does_not_exist.py"}, workspace
    )
    assert is_error is True


# --- edit_file preserves line endings ------------------------------------

def test_edit_file_preserves_crlf(workspace):
    target = workspace / "library" / "crlf.py"
    target.write_bytes(b"def add(a, b):\r\n    return a - b\r\n")

    _, is_error = tools.dispatch_tool(
        "edit_file",
        # the model sees LF (read_file uses universal newlines) and edits in LF
        {"path": "library/crlf.py", "old_string": "    return a - b\n", "new_string": "    # fixed\n    return a + b\n"},
        workspace,
    )

    assert not is_error
    assert target.read_bytes() == b"def add(a, b):\r\n    # fixed\r\n    return a + b\r\n"


def test_edit_file_preserves_lf(workspace):
    target = workspace / "library" / "lf.py"
    target.write_bytes(b"def add(a, b):\n    return a - b\n")

    _, is_error = tools.dispatch_tool(
        "edit_file", {"path": "library/lf.py", "old_string": "a - b", "new_string": "a + b"}, workspace
    )

    assert not is_error
    assert target.read_bytes() == b"def add(a, b):\n    return a + b\n"  # no CRLF, even on Windows


def test_edit_file_leaves_other_lines_of_mixed_file_alone(workspace):
    target = workspace / "library" / "mixed.py"
    target.write_bytes(b"a = 1\r\nb = 2\nc = 3\r\n")

    _, is_error = tools.dispatch_tool(
        "edit_file", {"path": "library/mixed.py", "old_string": "b = 2", "new_string": "b = 20"}, workspace
    )

    assert not is_error
    assert target.read_bytes() == b"a = 1\r\nb = 20\nc = 3\r\n"
