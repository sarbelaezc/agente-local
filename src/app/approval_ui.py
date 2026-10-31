from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ai.context_builder import ChatMessage, build_context
from ai.ollama_client import OllamaClient
from ai.prompts import build_prompt
from automation.edge_session import EdgeSession
from automation.teams_web import TeamsSafetyError, TeamsWebAutomation
from config.settings import Settings
from storage.database import AuditDatabase


class ApprovalWindow:
    def __init__(
        self,
        settings: Settings,
        database: AuditDatabase,
        stopping: dict[str, bool],
    ) -> None:
        self.settings = settings
        self.database = database
        self.stopping = stopping
        self.root = tk.Tk()
        self.root.title("Teams Local Agent - Human Approval")
        self.root.geometry("820x620")
        self.chat_id = tk.StringVar(value=settings.allowed_chats[0].id)
        self.approver = tk.StringVar()
        self.status = tk.StringVar(value="Paused - review required")
        self.draft = tk.StringVar()

        controls = ttk.Frame(self.root, padding=12)
        controls.pack(fill="x")
        ttk.Label(controls, text="Authorized chat:").pack(side="left")
        ttk.OptionMenu(
            controls,
            self.chat_id,
            self.chat_id.get(),
            *[chat.id for chat in settings.allowed_chats],
        ).pack(side="left", padx=8)
        ttk.Button(controls, text="Generate draft", command=self.generate).pack(side="right")

        body = ttk.Frame(self.root, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Draft (editable before approval):").pack(anchor="w")
        self.editor = tk.Text(body, height=18, wrap="word")
        self.editor.pack(fill="both", expand=True)
        ttk.Label(body, text="Approver:").pack(anchor="w", pady=(8, 0))
        ttk.Entry(body, textvariable=self.approver).pack(fill="x")

        actions = ttk.Frame(self.root, padding=12)
        actions.pack(fill="x")
        ttk.Button(actions, text="Approve and insert", command=self.approve_and_insert).pack(side="left")
        ttk.Button(actions, text="Reject", command=self.reject).pack(side="left", padx=8)
        ttk.Button(actions, text="Stop", command=self.stop).pack(side="right")
        ttk.Label(self.root, textvariable=self.status, padding=12).pack(fill="x")

    def run(self) -> None:
        self.root.mainloop()

    def stop(self) -> None:
        self.stopping["value"] = True
        self.status.set("Stopped")
        self.root.destroy()

    def generate(self) -> None:
        if self.stopping["value"]:
            return
        session = EdgeSession(
            executable=self.settings.edge_executable,
            profile_dir=self.settings.edge_profile_dir,
        )
        try:
            context = session.start()
            teams = TeamsWebAutomation(context, self.settings.teams_url, self.settings.allowed_chats)
            teams.open()
            chat = teams.open_chat(self.chat_id.get())
            raw_messages = teams.capture_messages(chat.id, self.settings.context_messages)
            messages = [
                ChatMessage(sender="participant", text=text, timestamp="")
                for text in raw_messages
            ]
            allowed_context = build_context(messages, self.settings.context_messages)
            client = OllamaClient(self.settings.ollama_url, self.settings.ollama_model)
            proposal = client.generate(build_prompt(allowed_context, "Respond in Spanish."))
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", proposal)
            self.status.set(f"Draft ready for {chat.name}; review required")
        except (TeamsSafetyError, OSError, ValueError) as exc:
            self.status.set(f"Stopped safely: {exc}")
            messagebox.showerror("Safety stop", str(exc))
        finally:
            session.close()

    def approve_and_insert(self) -> None:
        approver = self.approver.get().strip()
        final_text = self.editor.get("1.0", "end-1c")
        if not approver:
            messagebox.showerror("Approval required", "Enter the approver identity.")
            return
        if not final_text.strip():
            messagebox.showerror("Approval required", "The response cannot be empty.")
            return

        session = EdgeSession(
            executable=self.settings.edge_executable,
            profile_dir=self.settings.edge_profile_dir,
        )
        try:
            context = session.start()
            teams = TeamsWebAutomation(context, self.settings.teams_url, self.settings.allowed_chats)
            teams.open()
            chat = teams.open_chat(self.chat_id.get())
            teams.insert_draft(chat.id, final_text)
            self.database.record(
                chat.id,
                chat.name,
                "approved_inserted",
                final_text,
                final_text,
                approver,
            )
            if not messagebox.askyesno(
                "Explicit send confirmation",
                "Insert done. Do you want to send this exact text now?",
            ):
                self.status.set("Approved and inserted; send not confirmed")
                return
            teams.send(chat.id, final_text, approver)
            self.database.record(
                chat.id,
                chat.name,
                "sent",
                final_text,
                final_text,
                approver,
            )
            self.status.set("Sent only after explicit confirmation")
        except (TeamsSafetyError, OSError, ValueError) as exc:
            self.status.set(f"Stopped safely: {exc}")
            messagebox.showerror("Safety stop", str(exc))
        finally:
            session.close()

    def reject(self) -> None:
        chat = next(
            (chat for chat in self.settings.allowed_chats if chat.id == self.chat_id.get()),
            None,
        )
        if chat is not None:
            self.database.record(chat.id, chat.name, "rejected", self.editor.get("1.0", "end-1c"))
        self.editor.delete("1.0", "end")
        self.status.set("Draft rejected")
