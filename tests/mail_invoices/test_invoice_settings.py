import pytest
import yaml

from mail_invoices.settings import load_settings

INVOICES = {
    "opera_executable": "opera.exe",
    "profile_dir": "opera-profile",
    "workbook": "book.xlsx",
    "backup_dir": "backups",
    "approver": "Test",
    "ollama": {"url": "http://localhost:11434", "model": "invoice-model"},
}


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_unified_and_flat_config_are_equivalent(tmp_path):
    unified = _write(tmp_path, "unified.yaml", {"invoices": INVOICES})
    flat = _write(tmp_path, "flat.yaml", INVOICES)

    assert load_settings(unified) == load_settings(flat)
    assert load_settings(unified).ollama_url == "http://localhost:11434"
    assert load_settings(unified).ollama_model == "invoice-model"


def test_legacy_global_ollama_is_used_when_invoices_has_none(tmp_path):
    invoices = {key: value for key, value in INVOICES.items() if key != "ollama"}
    config = _write(tmp_path, "settings.yaml", {
        "invoices": invoices,
        "ollama": {"url": "http://localhost:11434", "model": "legacy-model"},
    })

    settings = load_settings(config)

    assert settings.ollama_url == "http://localhost:11434"
    assert settings.ollama_model == "legacy-model"


def test_unknown_sections_in_unified_config_are_ignored(tmp_path):
    config = _write(tmp_path, "settings.yaml", {
        "invoices": INVOICES,
        "teams": {"allowed_chats": []},
    })

    assert load_settings(config).approver == "Test"


@pytest.mark.parametrize("section", [None, [], "invalid"])
def test_unified_config_rejects_invalid_invoices_section(tmp_path, section):
    path = _write(tmp_path, "invalid.yaml", {"invoices": section})

    with pytest.raises(ValueError, match="invoices configuration must be a mapping"):
        load_settings(path)


def test_remote_ollama_url_is_rejected(tmp_path):
    remote = {**INVOICES, "ollama": {"url": "http://example.com:11434"}}
    path = _write(tmp_path, "remote.yaml", {"invoices": remote})

    with pytest.raises(ValueError):
        load_settings(path)


def test_local_paths_expand_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    data = {**INVOICES, "profile_dir": "$LOCALAPPDATA/MailInvoiceAgent/OperaProfile"}
    path = _write(tmp_path, "settings.yaml", {"invoices": data})

    assert load_settings(path).profile_dir == tmp_path / "MailInvoiceAgent" / "OperaProfile"


def test_unset_local_path_environment_variable_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("INVOICE_AGENT_TEST_PATH", raising=False)
    data = {**INVOICES, "profile_dir": "%INVOICE_AGENT_TEST_PATH%/OperaProfile"}
    path = _write(tmp_path, "settings.yaml", {"invoices": data})

    with pytest.raises(ValueError, match="environment variable"):
        load_settings(path)
