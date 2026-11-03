from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.parametrize("model,scan_code,read_only,panel_mode,code,scanned,opened", [
    ("local-test", 7, True, "existing", 7, True, False),
    ("missing-model", 0, False, "existing", 1, False, False),
    ("local-test", 0, False, "existing", 0, True, True),
    ("local-test", 7, False, "existing", 7, True, True),
    ("local-test", 0, False, "start", 0, True, True),
    ("local-test", 0, False, "wrong", 1, True, False),
    ("local-test", 7, False, "wrong", 7, True, False),
])
def test_powershell_launcher_reuses_service_and_preserves_scan_result(
    tmp_path, model, scan_code, read_only, panel_mode, code, scanned, opened,
):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    root = tmp_path / "project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    original = Path(__file__).parents[1] / "scripts" / "import-monthly-close.ps1"
    script = scripts / original.name
    script.write_text(
        original.read_text().replace(".venv\\Scripts\\python.exe", ".venv\\Scripts\\python.ps1")
        .replace("function Get-PanelContent {", "function Unused-PanelContent {"),
        encoding="utf-8",
    )
    fake = root / ".venv" / "Scripts" / "python.ps1"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        """if ($args -contains 'scan') {
    $args | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'scan.json')
    exit """ + str(scan_code) + """
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
    harness = root / "harness.ps1"
    harness.write_text(
        """$script:probes = 0
function Get-PanelContent {
    $script:probes++
    if ($env:PANEL_MODE -eq 'start' -and $script:probes -eq 1) {
        throw [System.Net.WebException]::new(
            'Refused', [System.Net.WebExceptionStatus]::ConnectFailure)
    }
    if ($env:PANEL_MODE -eq 'wrong') { return '<title>Other</title>' }
    return '<title>Cierre mensual - Panel local</title>'
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, [switch]$PassThru)
    if ($FilePath -eq 'http://127.0.0.1:8766') {
        Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'opened') -Value $FilePath
    } else {
        Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'panel-started') -Value $ArgumentList
        return [PSCustomObject]@{HasExited=$false; Id=-1}
    }
}
& (Join-Path $PSScriptRoot 'scripts\\import-monthly-close.ps1') """ +
        "-Start 2026-01-01 -End 2026-11-01" + (" -ReadOnly" if read_only else "")
        + "\nexit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [
                powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
                "-File", str(harness),
            ],
            env=os.environ | {"LOCALAPPDATA": str(root), "PANEL_MODE": panel_mode}, capture_output=True,
            text=True, timeout=30,
        )
        assert result.returncode == code, result.stderr
        assert (root / "scan.json").exists() is scanned
        assert (root / "opened").exists() is opened
        assert (root / "panel-started").exists() is (panel_mode == "start" and scanned)
        assert "Usando Ollama existente" in result.stdout
        assert "Cerrando la instancia" not in result.stdout
        if scanned:
            args = json.loads((root / "scan.json").read_text(encoding="utf-8-sig"))
            expected = ["--start", "2026-01-01", "--end", "2026-11-01"]
            if read_only:
                expected.append("--read-only")
            assert args[-len(expected):] == expected
        assert thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
