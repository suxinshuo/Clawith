from app.services.git_credential_helper import (
    detect_git_platform,
    build_git_auth_env,
    extract_repo_host,
    extract_owner_repo,
    get_credential_provider,
)


def test_detect_github_https():
    assert detect_git_platform("https://github.com/org/repo.git") == "github"


def test_detect_github_ssh():
    assert detect_git_platform("git@github.com:org/repo.git") == "github"


def test_detect_gitlab_https():
    assert detect_git_platform("https://gitlab.com/org/repo.git") == "gitlab"


def test_detect_gitlab_self_hosted():
    assert detect_git_platform("https://gitlab.mycompany.com/org/repo.git") == "gitlab"


def test_detect_unknown_platform():
    assert detect_git_platform("https://gitea.example.com/org/repo.git") == "gitea"


def test_extract_repo_host_https():
    assert extract_repo_host("https://github.com/org/repo.git") == "github.com"


def test_extract_repo_host_ssh():
    assert extract_repo_host("git@gitlab.com:org/repo.git") == "gitlab.com"


def test_build_git_auth_env_github():
    env = build_git_auth_env("https://github.com/org/repo.git", "mytoken123")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    import base64
    expected_b64 = base64.b64encode(b"x-access-token:mytoken123").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected_b64}"


def test_build_git_auth_env_gitlab():
    env = build_git_auth_env("https://gitlab.com/org/repo.git", "glpat-abc123")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.com/.extraheader"
    import base64
    expected_b64 = base64.b64encode(b"oauth2:glpat-abc123").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected_b64}"


def test_build_git_auth_env_empty_token():
    env = build_git_auth_env("https://github.com/org/repo.git", "")
    assert env == {}


def test_build_git_auth_env_none_token():
    env = build_git_auth_env("https://github.com/org/repo.git", None)
    assert env == {}


# extract_owner_repo tests

def test_extract_owner_repo_https_with_git_suffix():
    assert extract_owner_repo("https://github.com/myorg/myrepo.git") == ("myorg", "myrepo")


def test_extract_owner_repo_https_without_git_suffix():
    assert extract_owner_repo("https://github.com/myorg/myrepo") == ("myorg", "myrepo")


def test_extract_owner_repo_ssh():
    assert extract_owner_repo("git@github.com:myorg/myrepo.git") == ("myorg", "myrepo")


def test_extract_owner_repo_malformed():
    assert extract_owner_repo("not-a-valid-url") == ("", "")


# get_credential_provider tests

def test_get_credential_provider_github():
    assert get_credential_provider("https://github.com/org/repo.git") == "github"


def test_get_credential_provider_gitlab():
    assert get_credential_provider("https://gitlab.com/org/repo.git") == "gitlab"


def test_get_credential_provider_unknown():
    assert get_credential_provider("https://gitea.example.com/org/repo.git") == "gitea"
