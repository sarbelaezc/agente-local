from __future__ import annotations

import pytest

from mail_invoices import main, workflow
from mail_invoices.graph_mailbox import MailboxError
from mail_invoices.settings import MailSettings
from shared.storage import data_root


@pytest.fixture
def settings(tmp_path, monkeypatch):
    value = MailSettings("11111111-2222-3333-4444-555555555555", tmp_path / "book.xlsx", tmp_path, "Hoja1", 20)
    monkeypatch.setattr(main, "load_settings", lambda path: value)
    return value


def logs():
    return sorted((data_root() / "MailInvoiceAgent" / "logs").glob("*.log"))


def test_a_batch_run_leaves_its_log_with_progress_and_result(settings, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mail-invoices", "--resume-invoice-batch", "--no-model"])
    assert main.main() == 0
    [path] = logs()
    assert path.name.endswith("-resume.log")
    text = path.read_text(encoding="utf-8")
    assert "facturas: resume" in text
    assert "Sin correos pendientes para retomar" in text  # progress on standard error
    assert "No hay correos pendientes" in text  # the batch report is kept
    assert "Resultado: sin errores." in text and "fin: terminado" in text
    assert "Sin correos pendientes para retomar" in capsys.readouterr().err


def test_a_read_only_report_stays_on_screen_and_out_of_the_log(settings, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mail-invoices", "--scan-inbox"])

    def scan(*args):
        print('[{"sender": "proveedor@example.test", "subject": "Factura SYNTH-9"}]')
        return 0

    monkeypatch.setattr(workflow, "scan_inbox", scan)
    assert main.main() == 0
    assert "SYNTH-9" in capsys.readouterr().out
    text = logs()[0].read_text(encoding="utf-8")
    assert "facturas: scan-inbox" in text and "fin: terminado" in text
    assert "SYNTH-9" not in text and "proveedor@example.test" not in text


def test_a_stop_leaves_its_reason_and_place_in_the_log(settings, monkeypatch):
    monkeypatch.setattr("sys.argv", ["mail-invoices", "--scan-inbox"])

    def expired(*args):
        raise MailboxError("La sesion de Microsoft vencio; ejecuta --login")

    monkeypatch.setattr(workflow, "scan_inbox", expired)
    assert main.main() == 1
    text = logs()[0].read_text(encoding="utf-8")
    assert "Detenido: La sesion de Microsoft vencio" in text
    assert "Punto de parada: main.py" in text


def test_a_log_path_outside_the_invoice_logs_folder_is_refused(settings, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(workflow, "scan_inbox", lambda *args: ran.append(True) or 0)
    monkeypatch.setattr("sys.argv", [
        "mail-invoices", "--scan-inbox", "--log-file", str(tmp_path / "20261118-100000-batch.log"),
    ])
    assert main.main() == 1
    assert ran == [] and logs() == []


def test_only_the_last_ten_invoice_logs_are_kept(settings, monkeypatch):
    folder = data_root() / "MailInvoiceAgent" / "logs"
    folder.mkdir(parents=True)
    for day in range(1, 13):
        (folder / f"202611{day:02d}-100000-batch.log").write_text("anterior", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["mail-invoices", "--resume-invoice-batch"])
    main.main()
    names = [path.name for path in logs()]
    assert len(names) == 10 and "20261101-100000-batch.log" not in names
