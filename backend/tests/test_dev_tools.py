import pytest
from app.services.dev_tools import check_repo_allowed, truncate_output


def test_check_repo_allowed_exact_match():
    allowed = ["github.com/org/backend"]
    assert check_repo_allowed("https://github.com/org/backend.git", allowed) is True
    assert check_repo_allowed("https://github.com/org/frontend.git", allowed) is False


def test_check_repo_allowed_wildcard():
    allowed = ["github.com/org/*"]
    assert check_repo_allowed("https://github.com/org/backend.git", allowed) is True
    assert check_repo_allowed("https://github.com/org/frontend.git", allowed) is True
    assert check_repo_allowed("https://github.com/other/repo.git", allowed) is False


def test_check_repo_allowed_ssh_url():
    allowed = ["github.com/org/*"]
    assert check_repo_allowed("git@github.com:org/backend.git", allowed) is True


def test_check_repo_allowed_empty_list():
    assert check_repo_allowed("https://github.com/org/repo.git", []) is False


def test_truncate_output_short():
    output = "line1\nline2\nline3\n"
    assert truncate_output(output, head=200, tail=50) == output


def test_truncate_output_long():
    lines = [f"line {i}" for i in range(500)]
    output = "\n".join(lines)
    result = truncate_output(output, head=10, tail=5)
    assert "line 0" in result
    assert "line 9" in result
    assert "line 495" in result
    assert "省略" in result
