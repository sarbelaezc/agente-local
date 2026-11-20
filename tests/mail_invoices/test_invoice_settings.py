import pytest
import yaml

from mail_invoices.settings import load_settings

INVOICES = {
    "graph_client_id": "11111111-2222-3333-4444-555555555555",
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
    data = {**INVOICES, "backup_dir": "$LOCALAPPDATA/MailInvoiceAgent/Backups"}
    path = _write(tmp_path, "settings.yaml", {"invoices": data})

    assert load_settings(path).backup_dir == tmp_path / "MailInvoiceAgent" / "Backups"


def test_unset_local_path_environment_variable_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("INVOICE_AGENT_TEST_PATH", raising=False)
    data = {**INVOICES, "backup_dir": "%INVOICE_AGENT_TEST_PATH%/Backups"}
    path = _write(tmp_path, "settings.yaml", {"invoices": data})

    with pytest.raises(ValueError, match="environment variable"):
        load_settings(path)


def test_the_invoice_app_id_is_required_and_the_opera_keys_are_ignored(tmp_path):
    legacy = {key: value for key, value in INVOICES.items() if key != "graph_client_id"}
    legacy.update(opera_executable="opera.exe", profile_dir="opera-profile")
    with pytest.raises(ValueError, match="graph_client_id"):
        load_settings(_write(tmp_path, "legacy.yaml", {"invoices": legacy}))
    settings = load_settings(_write(tmp_path, "both.yaml", {"invoices": {**INVOICES, **legacy, "graph_client_id": INVOICES["graph_client_id"]}}))
    assert settings.graph_client_id == INVOICES["graph_client_id"] and settings.graph_authority == "consumers"


def test_the_invoice_authority_is_validated(tmp_path):
    assert load_settings(_write(tmp_path, "common.yaml", {**INVOICES, "graph_authority": "common"})).graph_authority == "common"
    with pytest.raises(ValueError, match="graph_authority"):
        load_settings(_write(tmp_path, "org.yaml", {**INVOICES, "graph_authority": "organizations"}))


def test_excluded_senders_accept_addresses_and_domains(tmp_path):
    from mail_invoices.settings import is_excluded_sender

    data = {**INVOICES, "excluded_senders": ["Invoice+Statements@mail.anthropic.com", "@tienda.example"]}
    rules = load_settings(_write(tmp_path, "settings.yaml", {"invoices": data})).excluded_senders
    assert rules == ("invoice+statements@mail.anthropic.com", "@tienda.example")
    assert is_excluded_sender("INVOICE+statements@mail.anthropic.com", rules)
    assert is_excluded_sender("ventas@tienda.example", rules)
    assert not is_excluded_sender("ventas@sub.tienda.example", rules)
    assert not is_excluded_sender("facturas@proveedor.example", rules)
    assert load_settings(_write(tmp_path, "none.yaml", {"invoices": INVOICES})).excluded_senders == ()
    for bad in (["no-es-correo"], "a@b.example"):
        with pytest.raises(ValueError, match="excluded_senders"):
            load_settings(_write(tmp_path, "bad.yaml", {"invoices": {**INVOICES, "excluded_senders": bad}}))
