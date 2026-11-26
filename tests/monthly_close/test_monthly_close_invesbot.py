import zlib
from datetime import date
from types import SimpleNamespace

import pytest
from pypdf.errors import PyPdfError

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


def test_a_file_that_cannot_be_read_names_the_file_but_not_its_folder(tmp_path):
    folder = tmp_path / "invesbot" / "santiago"
    (folder / "carpeta.pdf").mkdir(parents=True)  # read_bytes fails with an OSError
    with pytest.raises(invesbot.ReportError) as error:
        invesbot.read_reports(tmp_path)
    assert str(error.value) == "carpeta.pdf: no se pudo leer el archivo."


@pytest.mark.parametrize("failure", [
    PyPdfError("dañado"), ValueError("dañado"), KeyError("dañado"), IndexError("dañado"),
    TypeError("dañado"), zlib.error("dañado"),
])
def test_a_damaged_pdf_stops_naming_the_file(tmp_path, monkeypatch, failure):
    def broken(stream):
        raise failure

    monkeypatch.setattr(invesbot, "PdfReader", broken)
    folder = tmp_path / "invesbot" / "santiago"
    folder.mkdir(parents=True)
    (folder / "danado.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(invesbot.ReportError) as error:
        invesbot.read_reports(tmp_path)
    assert str(error.value) == "danado.pdf: PDF de informe ilegible."


def test_an_encrypted_report_says_so(monkeypatch):
    monkeypatch.setattr(invesbot, "PdfReader", lambda stream: SimpleNamespace(is_encrypted=True))
    with pytest.raises(invesbot.ReportError, match="cifrado"):
        invesbot._pdf_text(b"%PDF-1.4")


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


TRANSFER = "TRASLADO A FONDO DE INVERSION"
JULY = "INVESBOT:Plan Ficticio:2026-07-31:santiago"
AUGUST = "INVESBOT:Plan Ficticio:2026-08-31:santiago"
ONE_PLAN = {"santiago": {"Plan Ficticio"}}


def move(identifier, day, amount=100_000, merchant="Invesbot Plan Ficticio", decision="investment",
         owner="santiago", kind="transfer"):
    """A movement row as Store.invesbot_movements gives it, with the amount in pesos."""
    return {"id": identifier, "day": day, "merchant": merchant, "amount_minor": amount * 100,
            "kind": kind, "decision": decision, "owner": owner, "restorable": True}


def monthly(months=6, merchant="Invesbot Plan Ficticio"):
    """Plan contributions of 100.000 decided as investment, from January."""
    return [move(f"m{month}", f"2026-{month:02d}-15", merchant=merchant) for month in range(1, months + 1)]


def july_report(plan="Plan Ficticio"):
    return report(month=7, opening=100_000, additional=600_000, balance=710_000, plan=plan)


def august_report():
    return report(month=8, opening=100_000, additional=700_000, balance=815_000)


def test_the_spec_example_restores_july_and_counts_the_august_transfer():
    movements = [
        *monthly(),
        move("jul", "2026-07-01", merchant="Invesbot", decision="exclude"),
        move("tr", "2026-08-31", merchant=TRANSFER),
        move("ago", "2026-08-03", merchant="Invesbot", decision="exclude"),
    ]
    outcome = invesbot.plan_outcome([august_report(), july_report()], movements, ONE_PLAN, set())
    assert outcome.restore == ["jul"]
    assert outcome.differences[JULY] == (70000000, 70000000)
    assert outcome.differences[AUGUST] == (80000000, 80000000)


def test_a_missing_contribution_without_an_excluded_one_is_a_difference():
    outcome = invesbot.plan_outcome([july_report()], monthly(), ONE_PLAN, set())
    assert outcome.restore == []
    assert outcome.differences[JULY] == (70000000, 60000000)


def test_a_transfer_that_does_not_fit_is_another_fund():
    movements = [*monthly(), move("grande", "2026-07-02", amount=9_000_000, merchant=TRANSFER),
                 move("tr", "2026-07-20", merchant=TRANSFER)]
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, set())
    assert outcome.differences[JULY] == (70000000, 70000000)
    movements.pop()
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, set())
    assert outcome.differences[JULY] == (70000000, 60000000)


def test_a_transfer_counts_for_one_plan_only():
    movements = [*monthly(), *monthly(merchant="Invesbot Otro"), move("tr", "2026-07-02", merchant=TRANSFER)]
    plans = {"santiago": {"Plan Ficticio", "Otro"}}
    outcome = invesbot.plan_outcome([july_report(), july_report("Otro")], movements, plans, set())
    assert sorted(outcome.differences.values()) == [(70000000, 60000000), (70000000, 70000000)]


def test_a_restored_movement_the_person_reverted_is_left_alone():
    movements = [*monthly(), move("jul", "2026-07-01", decision="exclude")]
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, {"jul"})
    assert outcome.restore == []
    assert outcome.differences[JULY] == (70000000, 60000000)


@pytest.mark.parametrize("plans,restore,recorded", [
    ({"santiago": {"Plan Ficticio"}}, ["jul"], 70000000),
    ({"santiago": {"Plan Ficticio", "Otro"}}, [], 50000000),
])
def test_bare_invesbot_counts_only_with_one_plan(plans, restore, recorded):
    movements = [*monthly(5), move("jun", "2026-06-15", merchant="Invesbot"),
                 move("jul", "2026-07-01", merchant="Invesbot", decision="exclude")]
    outcome = invesbot.plan_outcome([july_report()], movements, plans, set())
    assert outcome.restore == restore
    assert outcome.differences[JULY] == (70000000, recorded)


def test_only_excluded_plan_contributions_are_restored():
    movements = [
        *monthly(),
        move("pendiente", "2026-07-01", decision=None),
        move("personal", "2026-07-02", decision="personal"),
        move("otro-plan", "2026-07-03", merchant="Invesbot Otro", decision="exclude"),
        move("de-ella", "2026-07-04", decision="exclude", owner="manuela"),
        move("traslado-de-ella", "2026-07-05", merchant=TRANSFER, owner="manuela"),
        move("antes", "2026-06-30", decision="exclude"),
        move("grande", "2026-07-06", amount=200_000, decision="exclude"),
    ]
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, set())
    assert outcome.restore == []
    assert outcome.differences[JULY] == (70000000, 60000000)


def test_a_restore_that_does_not_fit_is_skipped_and_later_ones_are_tried():
    movements = [*monthly(), move("grande", "2026-07-01", amount=200_000, decision="exclude"),
                 move("jul", "2026-07-20", decision="exclude")]
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, set())
    assert outcome.restore == ["jul"]
    assert outcome.differences[JULY] == (70000000, 70000000)


def test_a_movement_the_decision_would_refuse_is_never_restored():
    movements = [*monthly(), {**move("jul", "2026-07-01", decision="exclude"), "restorable": False}]
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, set())
    assert outcome.restore == []
    assert outcome.differences[JULY] == (70000000, 60000000)


@pytest.mark.parametrize("other,plans,blocked,restore", [
    (move("otro", "2026-03-10", merchant="Invesbot Sin Informe"), ONE_PLAN, True, []),
    (move("otro", "2026-03-10", merchant="Invesbot"), {"santiago": {"Plan Ficticio", "Otro"}}, True, []),
    (move("otro", "2026-03-10", merchant="Invesbot Sin Informe", decision="exclude"), ONE_PLAN, False, ["jul"]),
    (move("otro", "2026-03-10", merchant="Invesbot Sin Informe", owner="manuela"), ONE_PLAN, False, ["jul"]),
    (move("otro", "2026-03-10", merchant=TRANSFER), ONE_PLAN, False, ["jul"]),
    (move("otro", "2026-03-10", merchant="Invesbot"), ONE_PLAN, False, []),  # of the plan: nothing missing
])
def test_an_investment_of_no_reported_plan_stops_the_owner_corrections(other, plans, blocked, restore):
    movements = [*monthly(), other, move("jul", "2026-07-01", decision="exclude")]
    outcome = invesbot.plan_outcome([july_report()], movements, plans, set())
    assert outcome.restore == restore
    assert invesbot.has_unattributed(movements, "santiago", plans) is blocked


@pytest.mark.parametrize("transfer_day,restore", [("2026-07-10", []), ("2026-06-10", ["jul"])])
def test_a_pending_savings_transfer_of_the_month_stops_its_corrections(transfer_day, restore):
    movements = [*monthly(), move("tr", transfer_day, merchant=TRANSFER, decision=None),
                 move("jul", "2026-07-01", decision="exclude")]
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, set())
    assert outcome.restore == restore
    assert outcome.differences[JULY] == (70000000, 70000000 if restore else 60000000)


def test_movements_before_the_plan_started_do_not_count():
    movements = [move("viejo", "2026-01-12"), *monthly()]
    outcome = invesbot.plan_outcome([july_report()], movements, ONE_PLAN, set())
    assert outcome.differences[JULY] == (70000000, 60000000)


def insert(store, rows):
    with store.connect() as db:
        for row in rows:
            db.execute(
                """INSERT INTO transactions (id,message_id,day,merchant,account,amount_minor,currency,
                category,kind,decision,reason,owner) VALUES (:id,:id,:day,:merchant,:account,
                :amount_minor,:currency,'Inversiones',:kind,:decision,'Propuesta',:owner)""",
                {"currency": "COP", "account": "Cuenta A", **row},
            )


def test_the_movements_of_an_owner_are_read_by_merchant(make_store):
    store = make_store()
    insert(store, [
        move("a", "2026-07-01"), move("b", "2026-07-02", merchant=TRANSFER),
        move("c", "2026-07-03", merchant="Tienda"), move("d", "2026-07-04", owner="manuela"),
        {**move("e", "2026-07-05"), "currency": "USD"},
        move("f", "2026-07-06", merchant="Invesbotito"), move("g", "2026-07-07", merchant="Invesbot"),
    ])
    assert [row["id"] for row in store.invesbot_movements("santiago")] == ["a", "b", "g"]
    assert store.invesbot_movements("santiago")[0] == move("a", "2026-07-01")


def test_an_incoming_or_excluded_account_contribution_is_not_restored(make_store):
    store = make_store(excluded_accounts=["Cuenta B"])
    insert(store, [
        *monthly(),
        move("entra", "2026-07-01", decision="exclude", kind="income"),
        {**move("excluida", "2026-07-02", decision="exclude"), "account": "Cuenta B"},
        move("pago", "2026-07-03", decision="exclude", kind="card_payment"),
    ])
    movements = store.invesbot_movements("santiago")
    assert [m["id"] for m in movements if not m["restorable"]] == ["entra", "excluida", "pago"]
    reports = [july_report()]
    outcome = invesbot.plan_outcome(reports, movements, ONE_PLAN, set())
    assert outcome.restore == []
    assert store.apply_invesbot(invesbot.period_checks(reports), outcome) == 0


@pytest.mark.parametrize("kind", ["income", "loan", "card_payment", "transfer", "investment", "unknown"])
@pytest.mark.parametrize("account", ["Cuenta A", "Cuenta B"])
def test_restorable_agrees_with_the_decision(make_store, kind, account):
    store = make_store(excluded_accounts=["Cuenta B"])
    insert(store, [{**move("x", "2026-07-01", decision="exclude", kind=kind), "account": account}])
    [movement] = store.invesbot_movements("santiago")
    try:
        store.decide("x", "investment")
        accepted = True
    except ValueError:
        accepted = False
    assert movement["restorable"] is accepted


def test_applying_is_audited_and_repeating_changes_nothing(make_store):
    store = make_store()
    insert(store, [*monthly(), move("jul", "2026-07-01", merchant="Invesbot", decision="exclude")])
    reports = [july_report()]
    checks = invesbot.period_checks(reports)
    with store.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    def apply() -> int:
        outcome = invesbot.plan_outcome(
            reports, store.invesbot_movements("santiago"), ONE_PLAN, store.restored_invesbot_ids())
        return store.apply_invesbot(checks, outcome)

    assert apply() == 1
    assert apply() == 0
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == count
        row = db.execute("SELECT decision, category, note FROM transactions WHERE id='jul'").fetchone()
        audit = db.execute(
            "SELECT target, value FROM audit WHERE action='invesbot-restore'").fetchall()
    assert (row["decision"], row["category"]) == ("investment", "Inversiones")
    assert row["note"] == "Contado como aporte: el informe de Invesbot de julio de 2026 muestra el aporte."
    assert [tuple(entry) for entry in audit] == [("jul", JULY)]
    assert store.restored_invesbot_ids() == {"jul"}
    [stored] = [r for r in store.state()["statement_checks"] if r["product"] == "INVESBOT"]
    assert (stored["expected_contributions_minor"], stored["recorded_contributions_minor"]) == (
        70000000, 70000000)


def test_a_restore_keeps_the_existing_note(make_store):
    store = make_store()
    insert(store, [*monthly(), move("jul", "2026-07-01", decision="exclude")])
    with store.connect() as db:
        db.execute("UPDATE transactions SET note='Nota de la persona' WHERE id='jul'")
    reports = [july_report()]
    outcome = invesbot.plan_outcome(reports, store.invesbot_movements("santiago"), ONE_PLAN, set())
    assert store.apply_invesbot(invesbot.period_checks(reports), outcome) == 1
    with store.connect() as db:
        note = db.execute("SELECT note FROM transactions WHERE id='jul'").fetchone()["note"]
    assert note == ("Nota de la persona · Contado como aporte: el informe de Invesbot de julio "
                    "de 2026 muestra el aporte.")


def test_a_failed_correction_stores_nothing(make_store):
    store = make_store()
    insert(store, [*monthly(), move("jul", "2026-07-01", decision="exclude")])
    reports = [july_report()]
    outcome = invesbot.plan_outcome(reports, store.invesbot_movements("santiago"), ONE_PLAN, set())
    with store.connect() as db:
        db.execute("UPDATE transactions SET decision='personal' WHERE id='jul'")
    with pytest.raises(ValueError):
        store.apply_invesbot(invesbot.period_checks(reports), outcome)
    assert [r for r in store.state()["statement_checks"] if r["product"] == "INVESBOT"] == []
    assert store.restored_invesbot_ids() == set()


def stored_checks(store):
    return {r["suffix"] + r["period_end"]: r for r in store.state()["statement_checks"]
            if r["product"] == "INVESBOT"}


def test_complements_read_the_reports_after_the_statements(tmp_path, monkeypatch, make_store, preferences):
    from monthly_close import main as app
    store = make_store()
    insert(store, [*monthly(), move("jul", "2026-07-01", merchant="Invesbot", decision="exclude")])
    calls = []
    monkeypatch.setattr(app, "refresh_reconciled_coverage", lambda *args: calls.append(
        len(stored_checks(store))))
    monkeypatch.setattr(app.invesbot, "read_reports", lambda data_dir: [july_report()])
    app.run_complements(store, tmp_path, {}, preferences, date(2026, 3, 1))
    assert calls == [1]  # the rows were stored before the coverage was refreshed
    assert store.restored_invesbot_ids() == {"jul"}


def test_an_unreadable_report_stops_the_run_without_storing(tmp_path, monkeypatch, make_store, preferences):
    from monthly_close import main as app
    store = make_store()
    folder = tmp_path / "invesbot" / "santiago"
    folder.mkdir(parents=True)
    (folder / "bueno.pdf").write_bytes(b"bueno")
    (folder / "malo.pdf").write_bytes(b"malo")
    fake_pdf_text(monkeypatch, {"bueno": report_text(), "malo": "no es un informe"})
    with pytest.raises(invesbot.ReportError, match="malo.pdf"):
        app.run_complements(store, tmp_path, {}, preferences, date(2026, 3, 1))
    assert stored_checks(store) == {}


def test_without_reports_nothing_is_printed(tmp_path, make_store, preferences, capsys):
    from monthly_close import main as app
    store = make_store()
    app.run_complements(store, tmp_path, {}, preferences, date(2026, 3, 1))
    assert "Invesbot" not in capsys.readouterr().out


def test_the_lines_carry_counts_and_the_latest_difference_without_amounts(tmp_path, monkeypatch, make_store):
    from monthly_close import main as app
    store = make_store()
    insert(store, monthly(months=6))
    monkeypatch.setattr(app.invesbot, "read_reports", lambda data_dir: [july_report(), august_report()])
    lines = app.import_invesbot_reports(store, tmp_path)
    assert lines == [
        "Invesbot Plan Ficticio (santiago): 2 informes leídos; 0 aportes contados de nuevo.",
        "Invesbot Plan Ficticio: el informe de agosto cuenta más aportes que los registrados; revisa el panel.",
    ]
    assert not any("$" in line for line in lines)


def test_an_investment_of_no_reported_plan_is_told_and_nothing_is_restored(tmp_path, monkeypatch, make_store):
    from monthly_close import main as app
    store = make_store()
    insert(store, [*monthly(), move("otro", "2026-03-10", merchant="Invesbot Sin Informe"),
                   move("jul", "2026-07-01", decision="exclude")])
    monkeypatch.setattr(app.invesbot, "read_reports", lambda data_dir: [july_report()])
    lines = app.import_invesbot_reports(store, tmp_path)
    assert lines == [
        "Invesbot Plan Ficticio (santiago): 1 informes leídos; 0 aportes contados de nuevo.",
        "Invesbot Plan Ficticio: el informe de julio cuenta más aportes que los registrados; revisa el panel.",
        "Invesbot (santiago): hay aportes de Invesbot que no son de ningún plan con informe; "
        "no se corrige ninguno automáticamente.",
    ]
    assert store.restored_invesbot_ids() == set()


def test_an_older_report_added_later_recalculates_the_next(tmp_path, monkeypatch, make_store):
    from monthly_close import main as app
    store = make_store()
    newer = report(month=8, opening=100_000, additional=700_000, balance=815_000)
    older = report(month=7, opening=100_000, additional=600_000, balance=710_000)
    monkeypatch.setattr(app.invesbot, "read_reports", lambda data_dir: [newer])
    app.import_invesbot_reports(store, tmp_path)
    [first] = stored_checks(store).values()
    monkeypatch.setattr(app.invesbot, "read_reports", lambda data_dir: [older, newer])
    app.import_invesbot_reports(store, tmp_path)
    august = stored_checks(store)["Plan Ficticio2026-08-31"]
    assert first["contributions_minor"] == 80_000_000
    assert august["contributions_minor"] == 10_000_000
