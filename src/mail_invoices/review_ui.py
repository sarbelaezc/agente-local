from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from mail_invoices.documents import InvoiceCandidate, parse_money
from mail_invoices.settings import MailSettings
from mail_invoices.workbook import InvoiceWorkbook, RecordResult, ReviewedInvoice


@dataclass(frozen=True)
class ReviewItem:
    candidate: InvoiceCandidate
    message: str
    rejected_model_fields: tuple[str, ...] = ()
    model_error: str | None = None


def reviewed_invoice_from_fields(fields: dict[str, str]) -> ReviewedInvoice:
    try:
        issued = date.fromisoformat(fields["issued"].strip())
        amount: Decimal = parse_money(fields["amount"])
    except ValueError as error:
        raise ValueError(f"Fecha o valor inválido: {error}") from error
    invoice = ReviewedInvoice(
        company=fields["company"].strip(),
        legal_name=fields["legal_name"].strip(),
        issued=issued,
        amount=amount,
        number=fields["number"].strip(),
        currency=fields["currency"].strip().upper(),
    )
    invoice.validate()
    return invoice


def suggested_company(suggestions: dict[str, str], legal_name: str) -> str:
    """Returns the historical company for a legal name, or an empty string."""
    return suggestions.get(" ".join(legal_name.split()).casefold(), "")


def record_approved_batch(
    store: InvoiceWorkbook,
    invoices: list[ReviewedInvoice],
    *,
    approved: bool,
) -> list[RecordResult] | None:
    if not approved:
        return None
    return store.record(invoices)


class InvoiceReviewWindow:
    def __init__(
        self,
        items: list[ReviewItem],
        issues: list[dict],
        settings: MailSettings,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.settings = settings
        self.store = InvoiceWorkbook(settings.workbook, settings.backup_dir, settings.sheet)
        self.saved = False
        self.forms: list[dict] = []
        try:
            self.company_suggestions = self.store.company_suggestions()
            suggestion_error = None
        except (OSError, ValueError) as error:
            self.company_suggestions = {}
            suggestion_error = str(error)
        self.root = tk.Tk()
        self.root.title("Revisión de facturas - aprobación humana")
        self.root.geometry("1120x760")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self._build_header(issues)
        if suggestion_error:
            ttk.Label(
                self.root,
                text=f"No se pudo leer el historial para sugerir Empresa: {suggestion_error}",
                wraplength=1080,
                foreground="#8b1a1a",
                padding=(12, 0, 12, 8),
            ).pack(fill="x")
        self._build_cards(items)
        self._build_approval_controls()

    def _build_header(self, issues: list[dict]) -> None:
        ttk = self.ttk
        ttk.Label(
            self.root,
            text=(
                "Revise y edite cada factura. Seleccione explícitamente las filas "
                "que autoriza; no se guardará nada hasta confirmar el lote."
            ),
            wraplength=1080,
            padding=12,
        ).pack(fill="x")
        if not issues:
            return
        issue_text = "\n".join(
            f"{item.get('source', 'Adjunto')}: {item.get('error', 'requiere revisión')}"
            for item in issues
        )
        ttk.Label(
            self.root,
            text=f"Adjuntos no registrables (solo informativo):\n{issue_text}",
            wraplength=1080,
            foreground="#8b1a1a",
            padding=(12, 0, 12, 8),
        ).pack(fill="x")

    def _build_cards(self, items: list[ReviewItem]) -> None:
        tk = self.tk
        ttk = self.ttk
        body = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.cards = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.cards, anchor="nw"
        )
        self.cards.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            ),
        )
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(
                self.canvas_window, width=event.width
            ),
        )
        for index, item in enumerate(items, start=1):
            self._build_card(index, item)

    def _build_card(self, index: int, item: ReviewItem) -> None:
        tk = self.tk
        ttk = self.ttk
        candidate = item.candidate
        card = ttk.LabelFrame(
            self.cards, text=f"Factura {index}: {candidate.source}", padding=8
        )
        card.pack(fill="x", expand=True, pady=5)
        selected = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            card,
            text="Incluir y aprobar esta factura",
            variable=selected,
        ).grid(row=0, column=0, columnspan=6, sticky="w")
        review_text = "Requiere revisión manual" if candidate.requires_review else "Datos XML"
        ttk.Label(
            card,
            text=f"{review_text} · {candidate.method} · Correo: {item.message[:180]}",
            wraplength=1040,
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(2, 6))
        if item.rejected_model_fields or item.model_error:
            details = []
            if item.rejected_model_fields:
                details.append(
                    "Campos del modelo descartados: "
                    + ", ".join(item.rejected_model_fields)
                )
            if item.model_error:
                details.append(f"Error de Ollama: {item.model_error}")
            ttk.Label(
                card, text="; ".join(details), wraplength=1040, foreground="#8b1a1a"
            ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(0, 6))

        company = suggested_company(self.company_suggestions, candidate.legal_name)
        if company:
            ttk.Label(
                card,
                text="Empresa sugerida a partir del historial del Excel; verifíquela.",
                foreground="#1a4f8b",
            ).grid(row=5, column=0, columnspan=6, sticky="w")
        values = {
            "company": company,
            "legal_name": candidate.legal_name,
            "issued": candidate.issued.isoformat() if candidate.issued else "",
            "amount": str(candidate.amount) if candidate.amount is not None else "",
            "number": candidate.number,
            "currency": candidate.currency,
        }
        variables = {}
        fields = (
            ("company", "Empresa"),
            ("legal_name", "Razón social"),
            ("issued", "Fecha (AAAA-MM-DD)"),
            ("amount", "Valor"),
            ("number", "Número de factura"),
            ("currency", "Moneda"),
        )
        for position, (key, label) in enumerate(fields):
            row = 3 + position // 3
            column = (position % 3) * 2
            variable = tk.StringVar(value=values[key])
            variables[key] = variable
            ttk.Label(card, text=label).grid(
                row=row, column=column, sticky="w", padx=(0, 4), pady=4
            )
            ttk.Entry(card, textvariable=variable, width=30).grid(
                row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=4
            )
        for column in (1, 3, 5):
            card.columnconfigure(column, weight=1)
        self.forms.append({"selected": selected, "variables": variables, "item": item})

    def _build_approval_controls(self) -> None:
        tk = self.tk
        ttk = self.ttk
        approval = ttk.Frame(self.root, padding=12)
        approval.pack(fill="x")
        ttk.Label(approval, text="Aprobado por:").pack(side="left")
        self.approver = tk.StringVar(value=self.settings.approver)
        ttk.Entry(approval, textvariable=self.approver, width=32).pack(
            side="left", padx=8
        )
        ttk.Button(
            approval, text="Aprobar y guardar lote", command=self.approve_and_save
        ).pack(side="left")
        ttk.Button(
            approval, text="Cerrar sin guardar", command=self.root.destroy
        ).pack(side="right")

    def run(self) -> bool:
        self.root.mainloop()
        return self.saved

    def approve_and_save(self) -> None:
        from tkinter import messagebox

        selected = [form for form in self.forms if form["selected"].get()]
        if not selected:
            messagebox.showerror(
                "Aprobación requerida",
                "Seleccione al menos una factura para incluir en el lote.",
                parent=self.root,
            )
            return
        approver = self.approver.get().strip()
        if not approver:
            messagebox.showerror(
                "Aprobación requerida",
                "Escriba quién está aprobando el lote.",
                parent=self.root,
            )
            return
        try:
            invoices = [
                reviewed_invoice_from_fields(
                    {key: variable.get() for key, variable in form["variables"].items()}
                )
                for form in selected
            ]
            invoices = self._resolve_legacy_matches(invoices)
            if invoices is None:
                return
        except (OSError, ValueError) as error:
            messagebox.showerror("No se pudo validar el lote", str(error), parent=self.root)
            return

        approved = messagebox.askyesno(
            "Confirmar escritura",
            (
                f"¿Autoriza guardar {len(invoices)} factura(s) en este archivo?\n\n"
                f"{self.settings.workbook}\n\n"
                "Se creará una copia de seguridad antes de reemplazarlo."
            ),
            default=messagebox.NO,
            parent=self.root,
        )
        try:
            results = record_approved_batch(
                self.store, invoices, approved=approved
            )
        except (OSError, ValueError) as error:
            messagebox.showerror("No se pudo guardar el lote", str(error), parent=self.root)
            return
        if results is None:
            return
        inserted = sum(result.inserted for result in results)
        messagebox.showinfo(
            "Lote registrado",
            (
                f"Aprobado por: {approver}\n"
                f"Filas nuevas: {inserted}\n"
                f"Coincidencias existentes: {len(results) - inserted}"
            ),
            parent=self.root,
        )
        self.saved = True
        self.root.destroy()

    def _resolve_legacy_matches(
        self, invoices: list[ReviewedInvoice]
    ) -> list[ReviewedInvoice] | None:
        from tkinter import messagebox

        resolved = []
        for invoice in invoices:
            matches = self.store.legacy_matches(invoice)
            if len(matches) > 1:
                messagebox.showerror(
                    "Coincidencia histórica ambigua",
                    (
                        f"La factura coincide con varias filas sin número: {matches}. "
                        "Resuelva la ambigüedad en una revisión manual; el lote no se guardará."
                    ),
                    parent=self.root,
                )
                return None
            if not matches:
                resolved.append(invoice)
                continue
            row = matches[0]
            link_existing = messagebox.askyesno(
                "Posible duplicado histórico",
                (
                    f"Los datos coinciden con la fila {row}, que no tiene número de factura. "
                    "¿Desea asociar el número revisado a esa fila?"
                ),
                default=messagebox.NO,
                parent=self.root,
            )
            if link_existing:
                resolved.append(replace(invoice, legacy_row=row))
                continue
            add_new = messagebox.askyesno(
                "Confirmar factura nueva",
                (
                    f"¿Confirma que esta factura es nueva y debe agregarse pese a "
                    f"la coincidencia de la fila {row}?"
                ),
                default=messagebox.NO,
                parent=self.root,
            )
            if not add_new:
                return None
            resolved.append(replace(invoice, confirmed_new=True))
        return resolved


def run_invoice_review(
    items: list[ReviewItem], issues: list[dict], settings: MailSettings
) -> bool:
    if not items:
        raise ValueError("No hay facturas legibles para revisar")
    return InvoiceReviewWindow(items, issues, settings).run()
