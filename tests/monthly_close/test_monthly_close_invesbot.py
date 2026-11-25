from datetime import date

import pytest

from monthly_close import invesbot


def report_text(month="Julio", balance="762.345,67", returns="12.345,67", drop=None,
                withdrawals_label="RETIROS REALIZADOS", plan="Plan Ficticio"):
    lines = [
        "Invesbot | Reporte plan inversión", plan, f"{month} de 2026",
        f"Reporte plan inversión - {month} de 2026",
        "Fecha de inicio", "Ene 13 de 2026",
        "TOTAL DEL PLAN SALDO AL CIERRE DEL MES", f"$ {balance}",
        "MONTO DE APERTURA", "$ 50.000,00", "Ene 13 de 2026",
        "APORTES ADICIONALES", "$ 700.000,00", "7 aportes",
        withdrawals_label, "$ 0,00",
        "RENDIMIENTOS AL CIERRE DEL MES", f"$ {returns}",
        "Aportes último mes", "$ 0,00", "Rendimientos último mes", "$ 999,99",
    ]
    return "\n".join(line for line in lines if line != drop)


def test_a_report_gives_its_cumulative_figures():
    report = invesbot.parse_report(report_text(), "santiago")
    assert report == invesbot.Report(
        "santiago", "Plan Ficticio", date(2026, 7, 31), date(2026, 1, 13),
        76234567, 5000000, 70000000, 7, 0, 1234567,
    )


def test_ligatures_and_accents_do_not_matter():
    text = report_text(plan="Plan Fiﬁ Ficticio").replace("inversión", "inversion")
    assert invesbot.parse_report(text, "santiago").balance_minor == 76234567


@pytest.mark.parametrize("change,check", [
    ({"month": "Sptiembre"}, lambda r: r.period_end == date(2026, 9, 30)),
    ({"returns": "-97.181,60", "balance": "652.818,40"}, lambda r: r.returns_minor == -9718160),
    ({"withdrawals_label": "RETIROSREALIZADOS"}, lambda r: r.withdrawals_minor == 0),
])
def test_the_bank_wordings_are_read(change, check):
    assert check(invesbot.parse_report(report_text(**change), "santiago"))


@pytest.mark.parametrize("change,error", [
    ({"balance": "762.345,68"}, "no suman"),
    ({"drop": "MONTO DE APERTURA"}, "MONTO DE APERTURA"),
    ({"month": "Juliembre"}, "periodo"),
])
def test_a_report_that_is_not_exact_stops(change, error):
    with pytest.raises(invesbot.ReportError, match=error):
        invesbot.parse_report(report_text(**change), "santiago")


def fake_pdf_text(monkeypatch, texts):
    monkeypatch.setattr(invesbot, "_pdf_text", lambda content: texts[content.decode()])


def test_the_folders_are_read_by_owner_once_and_conflicts_stop(tmp_path, monkeypatch):
    texts = {"a": report_text(), "b": report_text(), "c": report_text(), "d": report_text(),
             "e": report_text(balance="762.345,68")}
    fake_pdf_text(monkeypatch, texts)
    for folder, name in [("santiago", "a"), ("santiago", "b"), ("manuela", "c"), ("otra", "d")]:
        (tmp_path / "invesbot" / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / "invesbot" / folder / f"{name}.pdf").write_bytes(name.encode())
    reports = invesbot.read_reports(tmp_path)
    assert [r.owner for r in reports] == ["manuela", "santiago"]
    (tmp_path / "invesbot" / "santiago" / "e.pdf").write_bytes(b"e")
    with pytest.raises(invesbot.ReportError, match="e.pdf"):
        invesbot.read_reports(tmp_path)


def test_a_pdf_that_is_not_a_report_stops_naming_the_file(tmp_path, monkeypatch):
    fake_pdf_text(monkeypatch, {"x": "hola"})
    folder = tmp_path / "invesbot" / "santiago"
    folder.mkdir(parents=True)
    (folder / "raro.pdf").write_bytes(b"x")
    with pytest.raises(invesbot.ReportError, match="raro.pdf"):
        invesbot.read_reports(tmp_path)


def test_without_folders_there_is_nothing_to_read(tmp_path):
    assert invesbot.read_reports(tmp_path) == []
