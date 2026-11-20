"""Local web panel where a person reviews and approves the invoices of a batch.

It listens on 127.0.0.1, on a free port, with the checks every local panel
shares (shared.local_panel): exact Host and Origin, a per-run CSRF token and small
JSON bodies. With `lan`, paired devices of the home network reach it over HTTPS. PDFs are served by a token of this batch, never by path. If the page
stops checking in (the tab was closed), the review ends without approving.
"""
from __future__ import annotations

import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from shared.lan_access import LanAccess, start_in_background
from shared.local_panel import LocalPanelHandler

PAGE = "review.html"
MAX_BODY = 512_000
# The page checks in every few seconds; silence this long means the tab was closed.
HEARTBEAT_SECONDS = 90
FIRST_VISIT_SECONDS = 600
REVIEW_LIMIT_SECONDS = 4 * 3600
DONE_GRACE_SECONDS = 300
# A PDF opens in the browser's own viewer, which a sandbox or script ban would break.
PDF_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


class ReviewPanel:
    """One review session: serve the page, wait for a decision, then report progress.

    view() returns what the page shows; actions maps a POST path to a function that
    receives the JSON body and returns a JSON-able result or raises ValueError. An
    action may finish the review by returning {"finish": True, ...}.
    """

    def __init__(
        self, view: Callable[[], dict], actions: dict[str, Callable[[dict], dict]],
        pdfs: dict[str, Path] | None = None, *, open_browser: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic, lan: LanAccess | None = None,
    ) -> None:
        self._view, self._actions = view, actions
        self._pdfs = dict(pdfs or {})
        # Resolved here, not at import, so a test can replace the system browser.
        self._open_browser, self._clock = open_browser or webbrowser.open, clock
        self._csrf = secrets.token_urlsafe(32)
        self._decided = threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self.phase = "review"
        self.approved = False
        self.result: dict = {}
        self._last_seen: float | None = None
        handler = self._handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._network = lan.serve(handler) if lan else None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/"

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._thread.start()
        if self._network is not None:
            start_in_background(self._network)
        self._open_browser(self.url)

    def wait_for_decision(self, poll: float = 1.0) -> bool:
        """Block until the person approves or closes; a closed tab counts as closing."""
        started = self._clock()
        while not self._decided.wait(poll):
            now = self._clock()
            seen = self._last_seen
            if seen is None and now - started > FIRST_VISIT_SECONDS:
                self._decide(False, "El panel no se abrió a tiempo; no se ejecutó nada.")
            elif seen is not None and now - seen > HEARTBEAT_SECONDS:
                self._decide(False, "Se cerró el panel sin aprobar; no se ejecutó nada.")
            elif now - started > REVIEW_LIMIT_SECONDS:
                self._decide(False, "La revisión superó el tiempo máximo; no se ejecutó nada.")
        return self.approved

    def run(self) -> bool:
        self.start()
        return self.wait_for_decision()

    def executing(self) -> None:
        with self._lock:
            self.phase = "executing"

    def finish(self, result: dict, wait: bool = True, poll: float = 1.0) -> None:
        """Show the outcome; stop when the page closes the panel, leaves, or after a grace period."""
        with self._lock:
            self.phase, self.result = "done", result
        started = self._clock()
        while wait and not self._closed.wait(poll):
            now = self._clock()
            seen = self._last_seen
            if now - started > DONE_GRACE_SECONDS or seen is None or now - seen > HEARTBEAT_SECONDS:
                break
        self.stop()

    def stop(self) -> None:
        for server in (self._server, self._network):
            if server is not None:
                server.shutdown()
                server.server_close()

    def _decide(self, approved: bool, message: str = "") -> None:
        with self._lock:
            self.approved = approved
            if not approved:
                self.phase, self.result = "closed", {"message": message}
        self._decided.set()

    # -------------------------------------------------------------- HTTP

    def _handler(self):
        panel = self

        class Handler(LocalPanelHandler):
            csrf = panel._csrf
            max_body = MAX_BODY

            def do_GET(self) -> None:
                if not self.allowed_get():
                    return
                path = self.route
                if path == "/":
                    self.respond(200, files("mail_invoices").joinpath(PAGE).read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/view":
                    panel._last_seen = panel._clock()
                    with panel._lock:
                        phase, result = panel.phase, dict(panel.result)
                    self.json_response(200, {**panel._view(), "phase": phase, "result": result, "csrf": panel._csrf})
                elif path.startswith("/api/pdf/") and path[9:] in panel._pdfs:
                    pdf = panel._pdfs[path[9:]]
                    try:
                        data = pdf.read_bytes()
                    except OSError:
                        self.json_response(404, {"error": "El PDF ya no está disponible."})
                        return
                    self.respond(
                        200, data, "application/pdf", PDF_CSP,
                        {"Content-Disposition": "inline; filename=factura.pdf"},
                    )
                else:
                    self.json_response(404, {"error": "Ruta inexistente."})

            def do_POST(self) -> None:
                if not self.allowed_post():
                    return
                try:
                    raw = self.read_json()
                except (ValueError, TypeError) as error:
                    self.json_response(400, {"error": str(error)})
                    return
                path = self.route
                if path == "/api/close":
                    self.close(raw)
                    return
                action = panel._actions.get(path)
                if action is None:
                    self.json_response(404, {"error": "Ruta inexistente."})
                    return
                with panel._lock:
                    if panel.phase != "review":
                        self.json_response(409, {"error": "La revisión ya terminó."})
                        return
                try:
                    result = action(raw) or {}
                except ValueError as error:
                    self.json_response(400, {"error": str(error), **getattr(error, "payload", {})})
                    return
                if result.pop("finish", False):
                    panel._decide(True)
                self.json_response(200, {"ok": True, **result})

            def close(self, raw: dict) -> None:
                closing = panel._actions.get("/api/close")
                with panel._lock:
                    phase = panel.phase
                if phase == "review":
                    if closing is not None:
                        try:
                            closing(raw)
                        except ValueError as error:
                            self.json_response(400, {"error": str(error)})
                            return
                    panel._decide(False, "Cerraste el panel sin aprobar; no se ejecutó nada.")
                panel._closed.set()
                self.json_response(200, {"ok": True})

        return Handler
