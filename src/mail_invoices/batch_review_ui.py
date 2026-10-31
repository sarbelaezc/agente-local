from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from mail_invoices.batch_state import BatchMessage, BatchStateStore
from mail_invoices.review import reviewed_invoice_from_fields
from mail_invoices.review_ui import InvoiceReviewWindow
from mail_invoices.settings import MailSettings

STATE_TEXT = {
    "pending": "Pendiente de aprobacion",
    "approved": "Aprobado; registro pendiente de verificar",
    "registered": "Registrado; archivo pendiente",
    "moving": "Movimiento incierto: solo se verificara el destino, NO se repetira",
    "moved": "Archivado y verificado",
    "blocked": "Bloqueado: requiere resolver el conflicto",
}


class BatchReviewWindow(InvoiceReviewWindow):
    def __init__(
        self, messages: list[BatchMessage], folders: list[str],
        settings: MailSettings, journal: BatchStateStore,
    ) -> None:
        self.messages, self.folders, self.journal = messages, folders, journal
        self.message_forms: list[dict] = []
        super().__init__([], [], settings)
        self.root.title("Revisar lote: registrar facturas y archivar correos")

    def _build_header(self, issues: list[dict]) -> None:
        self.ttk.Label(
            self.root,
            text="Revise TODOS los datos y el destino de cada correo. Las casillas empiezan "
            "desmarcadas. Aprobar y ejecutar autoriza registro en Excel, aprendizaje local "
            "de la carpeta y movimiento de los correos seleccionados, sin mas confirmaciones "
            "salvo duplicados historicos. Los pendientes previamente aprobados se retomaran.",
            wraplength=1080, padding=12,
        ).pack(fill="x")

    def _build_cards(self, items) -> None:
        super()._build_cards([])
        outer = self.cards
        for message in self.messages:
            group = self.ttk.LabelFrame(outer, text=message.subject[:180], padding=8)
            group.pack(fill="x", pady=6)
            self.ttk.Label(
                group, text=f"{STATE_TEXT[message.state]}\n{message.sender}\n"
                f"{message.reason}\n{message.last_error}\n" + "\n".join(message.errors),
                wraplength=1000,
            ).pack(fill="x")
            if message.state not in {"pending", "blocked"}:
                self.ttk.Label(
                    group, text=f"Destino previamente aprobado: {message.folder or 'Sin destino'}",
                ).pack(anchor="w")
                continue
            if message.state == "blocked":
                self.ttk.Label(
                    group, text="Corrija el conflicto en Excel o los datos revisados. "
                    "Marcar este correo y ejecutar requiere una NUEVA aprobacion; no cambia Excel al editar.",
                    wraplength=1000,
                ).pack(fill="x")
            selectable = bool(message.items) and not message.errors
            selected = self.tk.BooleanVar(value=False)
            folder = self.tk.StringVar(value=message.folder or "")
            self.ttk.Checkbutton(
                group, text="Aprobar TODAS las facturas de este correo y su archivo",
                variable=selected, state="normal" if selectable else "disabled",
            ).pack(anchor="w")
            self.ttk.Combobox(
                group, textvariable=folder, values=self.folders, state="readonly", width=65,
            ).pack(anchor="w", pady=5)
            form_start = len(self.forms)
            self.cards = group
            for index, item in enumerate(message.items, 1):
                self._build_card(index, item)
                form = self.forms[-1]
                if len(message.invoices) == len(message.items):
                    approved = message.invoices[index - 1]
                    values = {
                        "company": approved.company, "legal_name": approved.legal_name,
                        "issued": approved.issued.isoformat(), "amount": str(approved.amount),
                        "number": approved.number, "currency": approved.currency,
                    }
                    for key, value in values.items():
                        form["variables"][key].set(value)
                form["selected"] = selected
                card = group.winfo_children()[-1]
                # One checkbox per email: its invoices cannot be partially approved.
                for child in card.winfo_children():
                    if isinstance(child, self.ttk.Checkbutton):
                        child.destroy()
            self.message_forms.append({
                "message": message, "selected": selected, "folder": folder,
                "forms": self.forms[form_start:], "selectable": selectable,
            })
        self.cards = outer

    def _build_approval_controls(self) -> None:
        controls = self.ttk.Frame(self.root, padding=12)
        controls.pack(fill="x")
        self.ttk.Label(
            controls, text=f"Excel: {self.settings.workbook}\n"
            f"Respaldo antes de escribir. Estados locales: {self.journal.path}",
            wraplength=1080,
        ).pack(fill="x")
        self.ttk.Button(controls, text="Seleccionar correos aptos", command=self.select_all).pack(side="left")
        self.ttk.Label(controls, text="Aprobado por:").pack(side="left", padx=8)
        self.approver = self.tk.StringVar(value=self.settings.approver)
        self.ttk.Entry(controls, textvariable=self.approver, width=25).pack(side="left")
        self.ttk.Button(
            controls, text="Aprobar y ejecutar lote", command=self.approve_and_save,
        ).pack(side="left", padx=8)
        self.ttk.Button(controls, text="Cerrar sin ejecutar", command=self.root.destroy).pack(side="right")

    def select_all(self) -> None:
        for form in self.message_forms:
            form["selected"].set(form["selectable"])

    def approve_and_save(self) -> None:
        from dataclasses import replace
        from tkinter import messagebox

        selected = [form for form in self.message_forms if form["selected"].get()]
        executable = [message for message in self.messages if message.state in {"approved", "registered", "moving"}]
        try:
            if not selected and not executable:
                raise ValueError("Seleccione al menos un correo apto para aprobar")
            approver = self.approver.get().strip()
            if selected and not approver:
                raise ValueError("Indique quien aprueba el lote")
            staged = []
            for form in selected:
                message = form["message"]
                if not form["selectable"] or form["folder"].get() not in self.folders:
                    raise ValueError("Cada correo seleccionado requiere una carpeta existente y no tener errores")
                invoices = [
                    reviewed_invoice_from_fields({
                        key: variable.get() for key, variable in invoice_form["variables"].items()
                    })
                    for invoice_form in form["forms"]
                ]
                invoices = self._resolve_legacy_matches(invoices)
                if invoices is None:
                    return
                staged.append(replace(
                    message, folder=form["folder"].get(), invoices=invoices, approver=approver,
                    approved_at=datetime.now(timezone.utc).isoformat(), state="approved", last_error="",
                    context=replace(
                        message.context, supplier=invoices[0].legal_name,
                        invoices=tuple({
                            **context, "supplier": invoice.legal_name,
                        } for context, invoice in zip(message.context.invoices, invoices, strict=True)),
                    ),
                ))
            # All approvals persist atomically BEFORE any Excel or Outlook side effect.
            if staged:
                self.journal.save(staged)
            by_id = {message.conversation: message for message in staged}
            self.messages[:] = [by_id.get(message.conversation, message) for message in self.messages]
        except (OSError, ValueError, sqlite3.Error) as error:
            messagebox.showerror("No se aprobo el lote", str(error), parent=self.root)
            return
        self.saved = True
        self.root.destroy()
