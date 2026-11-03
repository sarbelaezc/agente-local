from __future__ import annotations

import sqlite3

from mail_invoices.classification_decisions import ClassificationDecisionStore
from mail_invoices.folders import ConfirmedExample, IncomingMessage

STATUS_TEXT = {
    "registered": "Registro verificado en Excel",
    "not_registered": "No registrada en Excel",
    "historical_review": "Coincidencia histórica sin número; requiere revisión",
    "conflict": "Conflicto con Excel",
    "incomplete": "Datos incompletos",
}


def review_classification(
    report: dict, store: ClassificationDecisionStore | None = None, approver: str = "",
) -> None:
    """Persist only explicitly checked decisions; never moves mail or writes Excel."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Simulación de clasificación - no mueve correos")
    root.geometry("1100x760")
    ttk.Label(
        root,
        text="Revise las carpetas propuestas. Esta ventana NO mueve correos ni registra "
        "facturas. Marque solo los destinos verificados y pulse Guardar decisiones confirmadas. "
        "Se conservan extractos locales para futuras sugerencias; cerrar no confirma nada.",
        wraplength=1060, padding=12,
    ).pack(fill="x")
    body = ttk.Frame(root)
    body.pack(fill="both", expand=True)
    canvas = tk.Canvas(body, highlightthickness=0)
    scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    cards = ttk.Frame(canvas)
    handle = canvas.create_window((0, 0), window=cards, anchor="nw")
    cards.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(handle, width=event.width))
    variables = []
    for message in report["messages"]:
        card = ttk.LabelFrame(cards, text=message["subject"] or "Correo no identificable", padding=10)
        card.pack(fill="x", padx=12, pady=6)
        lines = [f"Remitente: {message['sender']}"]
        lines.extend(
            f"{invoice['number'] or 'Sin número'}: {STATUS_TEXT[invoice['status']]}"
            for invoice in message["invoices"]
        )
        lines.extend(message["errors"])
        lines.append(
            "Registro apto para una prueba posterior de movimiento."
            if message["eligible"] else "Movimiento BLOQUEADO: pendientes o errores."
        )
        lines.append(f"Propuesta ({message.get('source', 'none')}): {message.get('reason', '')}")
        ttk.Label(card, text="\n".join(lines), wraplength=1040).pack(fill="x")
        variable = tk.StringVar(value=message["folder"] or "")
        ttk.Combobox(
            card, textvariable=variable, values=["", *report["folders"]],
            state="readonly", width=65,
        ).pack(anchor="w", pady=6)
        checked = tk.BooleanVar(value=False)
        if store is not None:
            ttk.Checkbutton(
                card, text="Confirmo esta carpeta como ejemplo para futuras sugerencias",
                variable=checked,
                state="normal" if "classification_context" in message else "disabled",
            ).pack(anchor="w")
        variables.append((message, variable, checked))

    def close() -> None:
        for message, variable, _ in variables:
            message["reviewed_folder"] = variable.get() or None
        root.destroy()

    ttk.Button(root, text="Cerrar simulación (sin mover)", command=close).pack(pady=10)
    if store is not None:
        ttk.Label(root, text=f"Decisiones locales: {store.path}", wraplength=1060).pack()
        ttk.Label(root, text="Confirmado por:").pack()
        approved_by = tk.StringVar(value=approver)
        ttk.Entry(root, textvariable=approved_by).pack()

        def save() -> None:
            selected = [
                (message, variable, checked)
                for message, variable, checked in variables if checked.get()
            ]
            examples = []
            for message, variable, _ in selected:
                raw = message["classification_context"]
                examples.append(ConfirmedExample(
                    IncomingMessage(
                        raw["sender"], raw["subject"], raw["supplier"], raw["body"],
                        tuple(raw["invoices"]),
                    ),
                    variable.get(),
                ))
            try:
                count = store.save(examples, approved_by.get(), report["folders"])
            except (OSError, sqlite3.Error, ValueError) as error:
                messagebox.showerror("No se guardaron las decisiones", str(error), parent=root)
                return
            for message, variable, checked in selected:
                message["confirmed_folder"] = variable.get()
                checked.set(False)
            messagebox.showinfo(
                "Decisiones guardadas", f"{count} decisiones guardadas localmente. No se movieron correos.",
                parent=root,
            )

        ttk.Button(root, text="Guardar decisiones confirmadas", command=save).pack(pady=8)
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
