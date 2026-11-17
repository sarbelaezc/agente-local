"""Local web panel that replaces the Tkinter review windows of the invoice organizer.

It listens on 127.0.0.1 only, on a free port, with the same protections as the
monthly close dashboard: exact Host and Origin, a per-run CSRF token and small
JSON bodies. PDFs are served by a token of this batch, never by path. If the page
stops checking in (the tab was closed), the review ends without approving.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

PAGE = "review.html"
MAX_BODY = 512_000
# The page checks in every few seconds; silence this long means the tab was closed.
HEARTBEAT_SECONDS = 90
FIRST_VISIT_SECONDS = 600
REVIEW_LIMIT_SECONDS = 4 * 3600
DONE_GRACE_SECONDS = 300
PANEL_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
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
        clock: Callable[[], float] = time.monotonic,
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
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/"

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._thread.start()
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
        self._server.shutdown()
        self._server.server_close()

    def _decide(self, approved: bool, message: str = "") -> None:
        with self._lock:
            self.approved = approved
            if not approved:
                self.phase, self.result = "closed", {"message": message}
        self._decided.set()

    # -------------------------------------------------------------- HTTP

    def _handler(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def valid_host(self) -> bool:
                return self.headers.get("Host") == f"127.0.0.1:{panel._server.server_port}"

            def respond(
                self, status: int, payload: bytes, content_type: str,
                csp: str = PANEL_CSP, extra: dict | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", csp)
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def json_response(self, status: int, value: dict) -> None:
                self.respond(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

            def do_GET(self) -> None:
                if not self.valid_host():
                    self.json_response(403, {"error": "Host no autorizado."})
                    return
                path = urlsplit(self.path).path
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
                origin = f"http://127.0.0.1:{panel._server.server_port}"
                if (
                    not self.valid_host() or self.headers.get("Origin") != origin
                    or not secrets.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), panel._csrf.encode())
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                    or self.headers.get("Transfer-Encoding") is not None
                ):
                    self.json_response(403, {"error": "Solicitud local no autorizada."})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError("Tamaño de solicitud inválido.")
                    raw = json.loads(self.rfile.read(length))
                    if not isinstance(raw, dict):
                        raise ValueError("Solicitud JSON inválida.")
                except (ValueError, TypeError) as error:
                    self.json_response(400, {"error": str(error)})
                    return
                path = urlsplit(self.path).path
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
