from __future__ import annotations

import json

from playwright.sync_api import Page

from mail_invoices.browser import reopen_outlook
from mail_invoices.inbox import MESSAGE_SELECTOR
from mail_invoices.outlook_folders import TREE_SELECTOR, discover_folders, message_metadata, read_message_body


def find_conversation(page: Page, identity: str):
    if not identity:
        raise LookupError("Missing conversation identity")
    selector = f'{MESSAGE_SELECTOR}[data-convid={json.dumps(identity)}]'
    for _ in range(60):
        found = page.locator(selector)
        if found.count() == 1:
            return found
        if found.count() > 1:
            raise LookupError("Conversation identity is ambiguous")
        rows = page.locator(MESSAGE_SELECTOR)
        if not rows.count():
            break
        changed = rows.last.evaluate(
            """item => {
                let box = item.parentElement;
                while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement;
                if (!box) return false;
                const before = box.scrollTop;
                box.scrollTop += box.clientHeight;
                return box.scrollTop !== before;
            }"""
        )
        page.wait_for_timeout(800)
        if not changed:
            break
    raise LookupError("Conversation is not available in the expected folder")


def select_move_destination(dialog, path: str) -> None:
    roots = dialog.locator('[role="treeitem"][aria-level="1"]')
    if roots.count() != 1:
        raise LookupError("Move dialog mailbox is missing or ambiguous")
    parent = roots
    for index, name in enumerate(path.split("/")):
        matches = parent.evaluate(
            """(el, name) => {
                const group = [...el.children].find(node => node.getAttribute('role') === 'group');
                return group ? [...group.children].filter(child =>
                    child.getAttribute('role') === 'treeitem' &&
                    [...child.querySelectorAll('[title]')].some(node =>
                        node.title === name && node.closest('[role="treeitem"]') === child)
                ).map(child => child.getAttribute('data-fui-tree-item-value')) : [];
            }""",
            name,
        )
        if len(matches) != 1:
            raise LookupError(f"Destination path is missing or ambiguous: {path}")
        if not matches[0]:
            raise LookupError("Destination has no stable folder identity")
        parent = dialog.locator(
            f'[role="treeitem"][data-fui-tree-item-value={json.dumps(matches[0])}]'
        )
        if parent.count() != 1:
            raise LookupError("Destination folder identity is ambiguous")
        title = parent.get_by_title(name, exact=True)
        title.click()
        if index < len(path.split("/")) - 1 and parent.get_attribute("aria-expanded") != "true":
            parent.press("ArrowRight")
            parent.locator(':scope > [role="group"]').wait_for()
    if parent.get_attribute("tabindex") != "0":
        raise LookupError("Destination selection could not be verified")


def move_registered_message(page: Page, message: dict, folder: str, *, approved: bool) -> None:
    if not approved or not message["eligible"]:
        raise ValueError("Movement requires verified registration and explicit approval")
    matches = [f for f in discover_folders(page) if f.path == folder]
    if len(matches) != 1:
        raise LookupError("Approved folder is unavailable")
    reopen_outlook(page, MESSAGE_SELECTOR)
    option = find_conversation(page, message["conversation"])
    if message_metadata(option) != (message["sender"], message["subject"]):
        raise LookupError("Conversation metadata changed; movement blocked")
    option.evaluate("el => el.click()")
    page.get_by_role("document", name="Cuerpo del mensaje", exact=True).wait_for()
    read_message_body(page)
    if option.get_attribute("aria-selected") != "true":
        raise LookupError("Expected conversation is not selected")
    if page.locator(f'{MESSAGE_SELECTOR}[aria-selected="true"]').count() != 1:
        raise LookupError("More than one conversation is selected; movement blocked")
    page.get_by_role("button", name="Mover a", exact=True).click()
    page.get_by_role("menuitem", name="Seleccionar una carpeta diferente", exact=True).click()
    dialog = page.get_by_role("dialog")
    select_move_destination(dialog, folder)
    # No retries after this side effect: a verification failure requires manual review.
    message["movement_attempted"] = True
    message["movement_status"] = "unverified"
    dialog.get_by_role("button", name="Mover", exact=True).click()
    dialog.wait_for(state="hidden")
    matches = [f for f in discover_folders(page) if f.path == folder]
    if len(matches) != 1:
        raise LookupError("Moved but destination verification failed; review Outlook manually")
    destination = page.locator(TREE_SELECTOR).nth(matches[0].index)
    destination.click()
    page.wait_for_function(
        "el => (el.innerText || '').includes('seleccionado')", arg=destination.element_handle(),
    )
    page.wait_for_timeout(1500)
    found = find_conversation(page, message["conversation"])
    if message_metadata(found) != (message["sender"], message["subject"]):
        raise LookupError("Destination message metadata differs; review Outlook manually")
    message["movement_status"] = "verified"


def approve_movement(message: dict, folders: list[str]) -> str | None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Aprobar movimiento de UN correo")
    root.geometry("850x330")
    ttk.Label(
        root, text=f"{message['subject']}\nRegistro de todas las facturas verificado en Excel.\n"
        "Revise el destino. Pulsar Aprobar y mover autoriza el movimiento real de este correo.",
        wraplength=800, padding=15,
    ).pack()
    folder = tk.StringVar(value=message.get("reviewed_folder") or "")
    ttk.Combobox(root, textvariable=folder, values=folders, state="readonly", width=65).pack()
    approved = []

    def confirm() -> None:
        if folder.get() not in folders:
            messagebox.showerror("Destino requerido", "Seleccione una carpeta existente.", parent=root)
            return
        approved.append(folder.get())
        root.destroy()

    ttk.Button(root, text="Aprobar y mover este correo", command=confirm).pack(pady=15)
    ttk.Button(root, text="Cancelar (no mover)", command=root.destroy).pack()
    root.mainloop()
    return approved[0] if approved else None
