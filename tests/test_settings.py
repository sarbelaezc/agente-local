import pytest
import yaml

from config.settings import load_settings
from mail_invoices.settings import load_settings as load_mail_settings


def test_unified_config_preserves_both_modules(tmp_path):
    teams = {
        "edge": {"executable": "edge.exe", "profile_dir": "edge-profile"},
        "storage": {"database_path": "audit.db"},
        "allowed_chats": [{"id": "test", "name": "Test"}],
        "ollama": {"url": "http://localhost:11435", "model": "teams-model"},
    }
    invoices = {
        "opera_executable": "opera.exe", "profile_dir": "opera-profile",
        "workbook": "book.xlsx", "backup_dir": "backups", "approver": "Test",
        "ollama": {"url": "http://localhost:11434", "model": "invoice-model"},
    }
    unified = tmp_path / "unified.yaml"
    unified.write_text(yaml.safe_dump({"teams": teams, "invoices": invoices}), encoding="utf-8")
    teams_path = tmp_path / "teams.yaml"
    teams_path.write_text(yaml.safe_dump(teams), encoding="utf-8")
    invoices_path = tmp_path / "invoices.yaml"
    invoices_path.write_text(yaml.safe_dump(invoices), encoding="utf-8")

    assert load_settings(unified) == load_settings(teams_path)
    assert load_mail_settings(unified) == load_mail_settings(invoices_path)
    assert load_settings(unified).ollama_url == "http://localhost:11435"
    assert load_mail_settings(unified).ollama_url == "http://localhost:11434"
    assert load_settings(unified).ollama_model == "teams-model"
    assert load_mail_settings(unified).ollama_model == "invoice-model"


def test_legacy_shared_ollama_is_assigned_only_to_invoices(tmp_path):
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump({
        "teams": {
            "edge": {"executable": "edge.exe", "profile_dir": "edge-profile"},
            "storage": {"database_path": "audit.db"},
            "allowed_chats": [{"id": "test", "name": "Test"}],
        },
        "invoices": {
            "opera_executable": "opera.exe", "profile_dir": "opera-profile",
            "workbook": "book.xlsx", "backup_dir": "backups",
        },
        "ollama": {"url": "http://localhost:11434", "model": "legacy-model"},
    }), encoding="utf-8")

    assert load_settings(config).ollama_url == "http://127.0.0.1:11435"
    assert load_mail_settings(config).ollama_url == "http://localhost:11434"
    assert load_mail_settings(config).ollama_model == "legacy-model"


def test_unified_config_rejects_shared_ollama_instance(tmp_path):
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump({
        "teams": {
            "edge": {"executable": "edge.exe", "profile_dir": "edge-profile"},
            "storage": {"database_path": "audit.db"},
            "allowed_chats": [{"id": "test", "name": "Test"}],
            "ollama": {"url": "http://127.0.0.1:11434"},
        },
        "invoices": {
            "opera_executable": "opera.exe", "profile_dir": "opera-profile",
            "workbook": "book.xlsx", "backup_dir": "backups",
            "ollama": {"url": "http://localhost:11434"},
        },
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="separate Ollama instances"):
        load_settings(config)
    with pytest.raises(ValueError, match="separate Ollama instances"):
        load_mail_settings(config)


@pytest.mark.parametrize("section", [None, [], "invalid"])
def test_unified_config_rejects_invalid_sections(tmp_path, section):
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump({"teams": section, "invoices": section}), encoding="utf-8")
    with pytest.raises(ValueError, match="teams configuration must be a mapping"):
        load_settings(path)
    with pytest.raises(ValueError, match="invoices configuration must be a mapping"):
        load_mail_settings(path)


def test_local_paths_expand_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    config = tmp_path / "settings.yaml"
    config.write_text(
                r"""
edge:
    executable: 'C:\Edge\msedge.exe'
    profile_dir: '%LOCALAPPDATA%\TeamsLocalAgent\EdgeProfile'
storage:
    database_path: '%LOCALAPPDATA%\TeamsLocalAgent\audit.db'
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
                r"""
edge:
    executable: 'C:\Edge\msedge.exe'
    profile_dir: '%TEAMS_AGENT_TEST_PATH%\EdgeProfile'
storage:
    database_path: '%LOCALAPPDATA%\audit.db'
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