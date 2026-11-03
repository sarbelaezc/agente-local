from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from mail_invoices import workflow, classification, movement, classification_ui
from mail_invoices.workbook import ReviewedInvoice


@pytest.mark.parametrize("outcome", ["moved", "blocked", "cancelled", "changed_excel", "move_error"])
def test_integrated_flow_moves_only_registered_and_explicitly_approved(
    monkeypatch, tmp_path, outcome,
):
    from mail_invoices.settings import MailSettings

    invoice = ReviewedInvoice("Hospital", "Hospital", date(2026, 10, 31), Decimal("100"), "TEST", "COP")
    message = {
        "eligible": outcome != "blocked", "errors": [], "subject": "Synthetic",
        "sender": "test@example.invalid", "conversation": "TEST-ID",
        "registered_invoices": [invoice], "classification_context": {},
    }
    report = {"messages": [message], "folders": ["Salud"], "moved": 0, "simulation": True}
    settings = MailSettings(tmp_path / "opera.exe", tmp_path, tmp_path / "book.xlsx", tmp_path, "Hoja1", 20)
    calls = []

    def simulate(config, days, history, use_model, *, register):
        assert config.batch_limit == 1 and register
        return report

    monkeypatch.setattr(classification, "simulate_classification", simulate)
    monkeypatch.setattr(classification_ui, "review_classification", lambda *args: None)
    monkeypatch.setattr(
        movement, "approve_movement",
        lambda *args: None if outcome == "cancelled" else "Salud",
    )
    monkeypatch.setattr(
        workflow.InvoiceWorkbook, "registration_status",
        lambda *args, **kwargs: "conflict" if outcome == "changed_excel" else "registered",
    )

    def move(page, candidate, folder, *, approved):
        assert approved and candidate["eligible"] and folder == "Salud"
        calls.append("move")
        candidate["movement_attempted"] = True
        if outcome == "move_error":
            raise LookupError("Verification failed")

    monkeypatch.setattr(movement, "move_registered_message", move)
    context = SimpleNamespace(close=lambda: calls.append("close"))
    page = SimpleNamespace(wait_for_selector=lambda *args, **kwargs: None)
    monkeypatch.setattr(workflow, "outlook_page", lambda ctx: page)

    class Session:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=lambda *args, **kwargs: context,
            ))

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", Session)
    result = workflow.process_one_invoice(settings, 30, True)
    assert result == (0 if outcome == "moved" else 1)
    assert report["moved"] == (1 if outcome == "moved" else 0)
    assert ("move" in calls) == (outcome in {"moved", "move_error"})
    assert ("close" in calls) == ("move" in calls)
    assert "registered_invoices" not in message
    assert "classification_context" not in message
    if outcome in {"changed_excel", "move_error"}:
        assert message["errors"]


@pytest.mark.parametrize("approved,eligible", [(False, True), (True, False), (False, False)])
def test_movement_gate_never_touches_page_without_both_approvals(approved, eligible):
    with pytest.raises(ValueError, match="registration"):
        movement.move_registered_message(None, {"eligible": eligible}, "Salud", approved=approved)


def test_integrated_mode_refuses_more_than_one_email():
    with pytest.raises(ValueError, match="exactly one"):
        classification.simulate_classification(
            SimpleNamespace(batch_limit=2), 30, [], False, register=True,
        )


def test_destination_waits_for_async_tree_before_validating():
    events = []

    class Roots:
        @property
        def first(self):
            return self

        def wait_for(self, *, state):
            assert state == "visible"
            events.append("loaded")

        def count(self):
            assert events == ["loaded"]
            return 0

    dialog = SimpleNamespace(locator=lambda selector: Roots())
    with pytest.raises(LookupError, match="mailbox"):
        movement.select_move_destination(dialog, "Sura - Comfama")
