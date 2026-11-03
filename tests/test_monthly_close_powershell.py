from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.parametrize("model,code,scanned", [
    ("local-test", 7, True), ("missing-model", 1, False),
])
def test_powershell_launcher_reuses_service_and_preserves_scan_result(tmp_path, model, code, scanned):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    root = tmp_path / "project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    original = Path(__file__).parents[1] / "scripts" / "import-monthly-close.ps1"
    script = scripts / original.name
    script.write_text(
        original.read_text().replace(".venv\\Scripts\\python.exe", ".venv\\Scripts\\python.ps1"),
        encoding="utf-8",
    )
    fake = root / ".venv" / "Scripts" / "python.ps1"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        """if ($args -contains 'scan') {
    $args | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'scan.json')
    exit 7
}
exit 0
""", encoding="utf-8",
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"models": [{"name": "local-test"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    data = root / "MonthlyCloseAgent"
    data.mkdir()
    (data / "runtime.json").write_text(json.dumps({
        "ollama_url": f"http://127.0.0.1:{server.server_port}", "ollama_model": model,
    }), encoding="utf-8")
    try:
        result = subprocess.run(
            [
                powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
                "-File", str(script), "-Start", "2026-01-01", "-End", "2026-11-01", "-ReadOnly",
            ],
            env=os.environ | {"LOCALAPPDATA": str(root)}, capture_output=True,
            text=True, timeout=30,
        )
        assert result.returncode == code, result.stderr
        assert (root / "scan.json").exists() is scanned
        assert "Usando Ollama existente" in result.stdout
        assert "Cerrando la instancia" not in result.stdout
        if scanned:
            args = json.loads((root / "scan.json").read_text(encoding="utf-8-sig"))
            assert args[-5:] == ["--start", "2026-01-01", "--end", "2026-11-01", "--read-only"]
        assert thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
