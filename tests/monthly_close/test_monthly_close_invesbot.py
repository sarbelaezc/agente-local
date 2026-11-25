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


def report(month=2, opening=50_000, additional=100_000, balance=150_500, withdrawals=0,
           plan="Plan Ficticio", owner="santiago"):
    """A report with figures in pesos (cents are derived)."""
    last = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return invesbot.Report(
        owner, plan, date(2026, month, last), date(2026, 1, 13), balance * 100, opening * 100,
        additional * 100, 1, withdrawals * 100, (balance - opening - additional + withdrawals) * 100,
    )


def test_period_figures_come_from_the_previous_report():
    feb = report(month=2, opening=50_000, additional=100_000, balance=150_500)
    may = report(month=5, opening=50_000, additional=400_000, balance=452_000)
    [first, second] = invesbot.period_checks([may, feb])
    assert (first["opening_minor"], first["contributions_minor"], first["returns_minor"]) == (
        0, 15000000, 50000)
    assert (second["opening_minor"], second["contributions_minor"], second["withdrawals_minor"],
            second["returns_minor"]) == (15050000, 30000000, 0, 150000)
    assert second["identity"] == "INVESBOT:Plan Ficticio:2026-05-31:santiago"
    assert (second["product"], second["suffix"], second["owner"]) == (
        "INVESBOT", "Plan Ficticio", "santiago")
    assert (second["period_start"], second["period_end"]) == ("2026-05-01", "2026-05-31")
    assert (second["movements"], second["matched"], second["missing"]) == (0, 0, 0)
    assert second["balance_minor"] == 45200000


def test_a_loss_and_withdrawals_are_read_from_the_difference():
    jan = report(month=1, opening=50_000, additional=100_000, balance=140_000)
    feb = report(month=2, opening=50_000, additional=100_000, balance=100_000, withdrawals=30_000)
    [_, second] = invesbot.period_checks([jan, feb])
    assert second["returns_minor"] == (100_000 - 140_000 - 0 + 30_000) * 100
    assert second["withdrawals_minor"] == 3000000
    assert second["contributions_minor"] == 0
    assert second["returns_minor"] == -1000000


def test_plans_and_owners_are_independent_and_ordered():
    rows = invesbot.period_checks([
        report(month=3, plan="B"), report(month=2, plan="A", owner="manuela"),
        report(month=2, plan="A"), report(month=1, plan="B"),
    ])
    assert [(r["owner"], r["suffix"], r["period_end"]) for r in rows] == [
        ("manuela", "A", "2026-02-28"), ("santiago", "A", "2026-02-28"),
        ("santiago", "B", "2026-01-31"), ("santiago", "B", "2026-03-31"),
    ]
    assert rows[1]["opening_minor"] == 0


def test_checks_are_stored_and_repeating_changes_nothing(make_store):
    store = make_store()
    checks = invesbot.period_checks([report(month=2), report(month=3, balance=160_000)])
    store.record_statements(checks, [])
    first = store.state()["statement_checks"]
    store.record_statements(checks, [])
    rows = store.state()["statement_checks"]
    assert [{k: v for k, v in row.items() if k != "checked_at"} for row in rows] == [
        {k: v for k, v in row.items() if k != "checked_at"} for row in first]
    invesbot_rows = [row for row in rows if row["product"] == "INVESBOT"]
    assert len(invesbot_rows) == 2
    assert invesbot_rows[1]["opening_minor"] == 15050000
    assert invesbot_rows[0]["expected_contributions_minor"] is None
    assert invesbot_rows[0]["recorded_contributions_minor"] is None
