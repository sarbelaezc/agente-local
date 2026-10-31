from config.settings import load_settings


def test_local_paths_expand_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    config = tmp_path / "settings.yaml"
    config.write_text(
        """
edge:
  executable: "C:\\\\Edge\\\\msedge.exe"
  profile_dir: "%LOCALAPPDATA%\\\\TeamsLocalAgent\\\\EdgeProfile"
storage:
  database_path: "%LOCALAPPDATA%\\\\TeamsLocalAgent\\\\audit.db"
ollama:
  url: http://127.0.0.1:11434
allowed_chats:
  - id: test
    name: Test chat
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.edge_profile_dir == str(tmp_path / "TeamsLocalAgent" / "EdgeProfile")
    assert settings.database_path == str(tmp_path / "TeamsLocalAgent" / "audit.db")


def test_unset_local_path_environment_variable_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("TEAMS_AGENT_TEST_PATH", raising=False)
    config = tmp_path / "settings.yaml"
    config.write_text(
        """
edge:
  executable: "C:\\\\Edge\\\\msedge.exe"
  profile_dir: "%TEAMS_AGENT_TEST_PATH%\\\\EdgeProfile"
storage:
  database_path: "%LOCALAPPDATA%\\\\audit.db"
ollama:
  url: http://127.0.0.1:11434
allowed_chats:
  - id: test
    name: Test chat
""",
        encoding="utf-8",
    )

    try:
        load_settings(config)
    except ValueError as exc:
        assert "unset environment variable" in str(exc)
    else:
        raise AssertionError("An unset environment variable was accepted")
