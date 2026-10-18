"""Git source authentication uses synthetic credentials and never contacts a remote."""

import base64
import json
import logging
import traceback

import pytest

from ouroboros import skill_source_git as transport

TOKEN = "synthetic-github-source-token"
ENCODED = base64.b64encode(f"x-access-token:{TOKEN}".encode("utf-8")).decode("ascii")


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    import ouroboros.config as config

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Synthetic test")
    return settings


@pytest.mark.parametrize("token_source", ["env", "settings", "none"])
@pytest.mark.parametrize("url,authenticated", [
    ("https://github.com/alice/ouroboros-skills", True),
    ("https://GitHub.com/alice/skills.git", True),
    ("http://github.com/alice/skills", False),
    ("https://github.com.example.invalid/alice/skills", False),
    ("https://git.example.invalid/alice/skills", False),
    ("ssh://git@github.com/alice/skills", False),
    ("git@github.com:alice/skills.git", False),
    ("file:///tmp/synthetic-skills.git", False),
])
def test_fetch_credentials_are_scoped_to_github_https(credentials, tmp_path, monkeypatch, token_source,
                                                     url, authenticated):
    if token_source == "env":
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    elif token_source == "settings":
        credentials.write_text(json.dumps({"GITHUB_TOKEN": TOKEN}), encoding="utf-8")
    calls = []

    def run(argv, *, cwd, env, **kwargs):
        calls.append((argv, env))
        if argv[1] == "init":
            (cwd / ".git").mkdir()
        return 0, "a" * 40, ""

    monkeypatch.setattr("supervisor.git_ops._run_git_process_bounded", run)
    target = tmp_path / "clone"
    assert transport.fetch_tree(target, url, "main") == "a" * 40
    assert transport.fetch_tree(target, url, "main") == "a" * 40  # refresh an existing clone too
    transport.fetch_signals(target, url=url)
    fetches = [env for argv, env in calls if argv[1] == "fetch"]
    assert len(fetches) == 3
    for env in fetches:
        assert env["GIT_CONFIG_KEY_0"] == "user.name" and env["GIT_CONFIG_VALUE_0"] == "Synthetic test"
        config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                  for i in range(int(env["GIT_CONFIG_COUNT"]))}
        headers = {key: value for key, value in config.items() if key.lower().endswith(".extraheader")}
        assert headers == ({"http.https://github.com/.extraheader": f"AUTHORIZATION: basic {ENCODED}"}
                           if authenticated and token_source != "none" else {})
        assert env["GIT_TERMINAL_PROMPT"] == "0"
    for argv, _env in calls:
        assert TOKEN not in repr(argv) and ENCODED not in repr(argv)
    assert not any(path.is_file() for path in target.rglob("*")), "credentials must not be persisted"


@pytest.mark.parametrize("failure", ["stderr", "stdout", "exception"])
def test_fetch_errors_redact_credentials_from_messages_logs_and_cache(credentials, tmp_path, monkeypatch, caplog, failure):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    message = f"synthetic failure: {TOKEN} AUTHORIZATION: basic {ENCODED}"

    def run(argv, **kwargs):
        if argv[1] != "fetch":
            return 0, "", ""
        if failure == "exception":
            raise RuntimeError(message)
        return (1, message, "") if failure == "stdout" else (1, "", message)

    monkeypatch.setattr("supervisor.git_ops._run_git_process_bounded", run)
    row = {"id": "mine", "url": "https://github.com/alice/ouroboros-skills"}
    with pytest.raises(transport.SkillSourceFetchError):
        try:
            transport.fetch_tree(tmp_path / "direct", row["url"], "")
        except Exception:
            trace = traceback.format_exc()
            assert TOKEN not in trace and ENCODED not in trace
            raise
    with caplog.at_level(logging.INFO, logger=transport.__name__):
        meta = transport.update_clone(tmp_path / "data", row)
    assert "synthetic failure" in meta["error"]
    rendered = json.dumps(meta) + caplog.text + (transport.repo_cache_dir(tmp_path / "data", row) / "meta.json").read_text(encoding="utf-8")
    assert TOKEN not in rendered and ENCODED not in rendered
