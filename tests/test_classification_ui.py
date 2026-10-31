import subprocess
import sys
import textwrap
import pytest


def test_simulation_ui_reviews_folder_without_moving():
    script = textwrap.dedent("""
        import tkinter as tk
        from tkinter import ttk
        from mail_invoices.classification_ui import review_classification

        report = {
            "moved": 0, "folders": ["Compras/Recibos"],
            "messages": [{
                "subject": "Synthetic", "sender": "test@example.invalid",
                "invoices": [{"number": "TEST", "status": "not_registered"}],
                "eligible": False, "errors": [], "folder": "Compras/Recibos",
            }],
        }
        original = tk.Tk
        def create():
            root = original()
            root.withdraw()
            def close():
                for child in root.winfo_children():
                    if isinstance(child, ttk.Button):
                        child.invoke()
                        return
                raise AssertionError("Close simulation button missing")
            root.after(100, close)
            return root
        tk.Tk = create
        review_classification(report)
        assert report["moved"] == 0
        assert report["messages"][0]["reviewed_folder"] == "Compras/Recibos"
        assert not report["messages"][0]["eligible"]
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("action", ["save", "close", "empty_folder", "no_selection", "write_error"])
def test_only_explicit_confirmation_persists(tmp_path, action):
    script = textwrap.dedent("""
        import sys
        import tkinter as tk
        from tkinter import ttk, messagebox
        from dataclasses import asdict
        from pathlib import Path
        from mail_invoices.classification_ui import review_classification
        from mail_invoices.classification_decisions import ClassificationDecisionStore
        from mail_invoices.folders import IncomingMessage

        action = sys.argv[1]
        path = Path(sys.argv[2])
        if action == "write_error":
            path.write_text("invalid database")
        store = ClassificationDecisionStore(path)
        report = {
            "moved": 0, "folders": ["Salud"],
            "messages": [{
                "subject": "Synthetic", "sender": "test@example.invalid",
                "invoices": [{"number": "TEST", "status": "not_registered"}],
                "eligible": False, "errors": [], "folder": "Salud",
                "classification_context": asdict(IncomingMessage(
                    "test@example.invalid", "Synthetic", body="Consulta",
                )),
            }],
        }
        errors, successes = [], []
        messagebox.showerror = lambda *args, **kw: errors.append(args)
        messagebox.showinfo = lambda *args, **kw: successes.append(args)
        original = tk.Tk
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        def create():
            root = original()
            root.withdraw()
            def operate():
                widgets = list(descendants(root))
                checkbox = next(w for w in widgets if isinstance(w, ttk.Checkbutton))
                combo = next(w for w in widgets if isinstance(w, ttk.Combobox))
                buttons = [w for w in widgets if isinstance(w, ttk.Button)]
                if action != "no_selection":
                    checkbox.invoke()
                if action == "empty_folder":
                    combo.set("")
                if action != "close":
                    next(w for w in buttons if w.cget("text") == "Guardar decisiones confirmadas").invoke()
                next(w for w in buttons if w.cget("text") == "Cerrar simulación (sin mover)").invoke()
            root.after(100, operate)
            return root
        tk.Tk = create
        review_classification(report, store, "Santi")
        assert report["moved"] == 0
        assert not report["messages"][0]["eligible"]
        if action == "save":
            assert store.load()[0].folder == "Salud"
            assert report["messages"][0]["confirmed_folder"] == "Salud"
            assert len(successes) == 1 and not errors
        else:
            assert "confirmed_folder" not in report["messages"][0]
            assert not successes
            assert bool(errors) == (action != "close")
            if action == "write_error":
                assert path.read_text() == "invalid database"
            else:
                assert not path.exists()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, action, str(tmp_path / "decisions.sqlite3")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
