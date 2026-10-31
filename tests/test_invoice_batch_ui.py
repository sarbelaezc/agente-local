import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("action", [
    "approve", "close", "no_folder", "no_approver", "blocked", "invalid_field",
    "reapprove", "blocked_close",
])
def test_unified_batch_review_requires_complete_explicit_approval(tmp_path, action):
    script = textwrap.dedent("""
        import sys
        import tkinter as tk
        from tkinter import messagebox
        from pathlib import Path
        from datetime import date
        from decimal import Decimal
        from dataclasses import asdict
        from openpyxl import Workbook
        from mail_invoices.batch_state import BatchMessage, BatchStateStore
        from mail_invoices.batch_review_ui import BatchReviewWindow
        from mail_invoices.documents import InvoiceCandidate
        from mail_invoices.folders import IncomingMessage
        from mail_invoices.review import ReviewItem
        from mail_invoices.settings import MailSettings
        from mail_invoices.workbook import ReviewedInvoice

        directory = Path(sys.argv[1])
        action = sys.argv[2]
        path = directory / "book.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Hoja1"
        for column, header in enumerate(("Empresa", "Raz\\u00f3n Social", "Fecha", "Valor"), 2):
            sheet.cell(2, column, header)
        workbook.save(path)
        workbook.close()
        original = path.read_bytes()
        candidates = [
            InvoiceCandidate("synthetic.xml", "digest", number, "Synthetic SAS",
                date(2026, 10, 31), Decimal("100"), "COP", method="xml")
            for number in ["TEST-1", "TEST-2"]
        ]
        messages = [BatchMessage(
            "TEST", "sender@example.invalid", "Factura synthetic",
            [ReviewItem(candidate, "Factura synthetic") for candidate in candidates],
            IncomingMessage("sender@example.invalid", "Factura synthetic", invoices=tuple(
                {"supplier": "Synthetic SAS", "descriptions": "Consulta", "evidence": ""}
                for candidate in candidates
            )),
            folder="Salud", errors=["Unreadable document"] if action == "blocked" else [],
        )]
        if action in ("reapprove", "blocked_close"):
            messages[0].state = "blocked"
            messages[0].last_error = "Company conflict"
            messages[0].approver = "Previous"
            messages[0].invoices = [
                ReviewedInvoice("Approved company", "Synthetic SAS", date(2026,10,31),
                    Decimal("100"), candidate.number, "COP") for candidate in candidates
            ]
        settings = MailSettings(directory / "opera", directory, path, directory / "backups", "Hoja1", 3, approver="Santi")
        journal = BatchStateStore(directory / "states.sqlite3", "test")
        journal.save(messages)
        errors = []
        messagebox.showerror = lambda *a, **kw: errors.append(a)
        window = BatchReviewWindow(messages, ["Salud"], settings, journal)
        window.root.withdraw()
        assert not window.message_forms[0]["selected"].get()
        assert window.forms[0]["selected"] is window.forms[1]["selected"]
        if action in ("reapprove", "blocked_close"):
            assert window.forms[0]["variables"]["company"].get() == "Approved company"
        def act():
            if action not in ("close", "blocked_close"):
                window.select_all()
                if action == "reapprove":
                    for form in window.forms:
                        form["variables"]["company"].set("Corrected company")
                if action == "no_folder":
                    window.message_forms[0]["folder"].set("")
                if action == "no_approver":
                    window.approver.set("")
                if action == "invalid_field":
                    window.forms[1]["variables"]["amount"].set("not a number")
                window.approve_and_save()
            if not window.saved:
                window.root.destroy()
        window.root.after(100, act)
        assert window.run() == (action in ("approve", "reapprove"))
        loaded = journal.load()[0]
        assert path.read_bytes() == original
        assert not (directory / "backups").exists()
        if action in ("approve", "reapprove"):
            assert loaded.state == "approved"
            assert loaded.folder == "Salud" and loaded.approver == "Santi"
            assert len(loaded.invoices) == 2
            assert loaded.approved_at.endswith("+00:00")
            assert messages[0].state == "approved"
            assert not errors
            if action == "reapprove":
                assert all(invoice.company == "Corrected company" for invoice in loaded.invoices)
                assert loaded.last_error == ""
        elif action == "blocked_close":
            assert loaded.state == "blocked"
            assert loaded.approver == "Previous"
            assert loaded.last_error == "Company conflict"
            assert not errors
        else:
            assert loaded.state == "pending"
            assert not loaded.invoices
            assert bool(errors) == (action != "close")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), action],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
