from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.parametrize("model,scan_code,read_only,panel_mode,code,scanned,opened,network", [
    ("local-test", 7, True, "existing", 7, True, False, None),
    ("missing-model", 0, False, "existing", 1, False, False, None),
    ("local-test", 0, False, "existing", 0, True, True, None),
    ("local-test", 7, False, "existing", 7, True, True, None),
    ("local-test", 0, False, "start", 0, True, True, None),
    ("local-test", 0, False, "wrong", 1, True, False, None),
    ("local-test", 7, False, "wrong", 7, True, False, None),
    # import -Red: what listens on the panel ports (8766 local, 8767 network).
    ("local-test", 0, False, "start", 0, True, True, "none"),
    ("local-test", 0, False, "existing", 0, True, True, "shared"),
    ("local-test", 0, False, "existing", 1, False, False, "local"),
    ("local-test", 0, False, "existing", 1, False, False, "other"),
    ("local-test", 0, False, "existing", 1, True, False, "late"),
])
def test_powershell_launcher_reuses_service_and_preserves_scan_result(
    tmp_path, model, scan_code, read_only, panel_mode, code, scanned, opened, network,
):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    root = tmp_path / "project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    original = Path(__file__).parents[2] / "scripts" / "agente-local.ps1"
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
    @{host=$env:OLLAMA_HOST; models=$env:OLLAMA_MODELS} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'ollama-env.json')
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
    data = root / ".local" / "MonthlyCloseAgent"
    data.mkdir(parents=True)
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
function Get-NetTCPConnection {
    param($State, $LocalPort, $ErrorAction)
    $mode = $env:PANEL_NETWORK
    if ($mode -eq 'late') {
        $mode = if (Test-Path (Join-Path $env:LOCALAPPDATA 'scan.json')) { 'local' } else { 'none' }
    }
    if ($LocalPort -eq 8766 -and $mode -in @('shared', 'local', 'other')) { [PSCustomObject]@{OwningProcess=10} }
    if ($LocalPort -eq 8767 -and $mode -eq 'shared') { [PSCustomObject]@{OwningProcess=10} }
    if ($LocalPort -eq 8767 -and $mode -eq 'other') { [PSCustomObject]@{OwningProcess=20} }
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
& (Join-Path $PSScriptRoot 'scripts\\agente-local.ps1') import """ +
        "-Start 2026-01-01 -End 2026-11-01" + (" -ReadOnly" if read_only else "") + (" -Red" if network else "")
        + "\nexit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [
                powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
                "-File", str(harness),
            ],
            env=os.environ | {"LOCALAPPDATA": str(root), "PANEL_MODE": panel_mode, "AGENTE_LOCAL_DATA": "",
                              "PANEL_NETWORK": network or ""}, capture_output=True,
            text=True, timeout=30,
        )
        assert result.returncode == code, result.stderr
        assert (root / "scan.json").exists() is scanned
        assert (root / "opened").exists() is opened
        assert (root / "panel-started").exists() is (panel_mode == "start" and scanned)
        if panel_mode == "start":
            started = (root / "panel-started").read_text(encoding="utf-8-sig")
            assert ("--lan" in started) is bool(network)
        if network and not scanned:  # stops before starting anything
            assert "ejecuta import sin -Red" in "".join(result.stderr.split("\n")).replace("\r", "")
            assert "Ollama" not in result.stdout
        else:
            assert "Usando Ollama existente" in result.stdout
        assert "Cerrando la instancia" not in result.stdout
        if scanned:
            args = json.loads((root / "scan.json").read_text(encoding="utf-8-sig"))
            expected = ["--start", "2026-01-01", "--end", "2026-11-01"]
            if read_only:
                expected.append("--read-only")
            assert args[-len(expected):] == expected
            assert "--lan" not in args  # only the panel opens to the network, never the scan
            ollama_env = json.loads(
                (root / "ollama-env.json").read_text(encoding="utf-8-sig")
            )
            assert ollama_env == {
                "host": f"http://127.0.0.1:{server.server_port}",
                "models": os.environ.get("OLLAMA_MODELS"),
            }
        assert thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("arguments,expected", [
    (["owner-account", "-Owner", "manuela", "-Suffix", "1234"],
     ["owner-account", "--owner", "manuela", "--suffix", "1234"]),
    (["summary", "-Month", "2026-03", "-Owner", "hogar"],
     ["summary", "--month", "2026-03", "--owner", "hogar"]),
    (["serve"], ["serve"]),
    (["login", "-Mailbox", "manuela"], ["login", "--mailbox", "manuela"]),
    (["merge-category", "-FromCategory", "Predial", "-ToCategory", "Vivienda"],
     ["merge-category", "--from", "Predial", "--to", "Vivienda"]),
    (["payroll-deduction", "-Owner", "santiago", "-Suffix", "1234"],
     ["payroll-deduction", "--owner", "santiago", "--suffix", "1234"]),
])
def test_household_commands_pass_arguments_without_ollama(tmp_path, arguments, expected):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    original = Path(__file__).parents[2] / "scripts" / "agente-local.ps1"
    (root / "scripts" / original.name).write_text(
        original.read_text().replace(".venv\\Scripts\\python.exe", ".venv\\Scripts\\python.ps1"),
        encoding="utf-8",
    )
    fake = root / ".venv" / "Scripts" / "python.ps1"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "$args | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'args.json')\n"
        "exit 0\n", encoding="utf-8",
    )
    # No runtime.json: a command that needed Ollama would fail before calling Python.
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
         "-File", str(root / "scripts" / original.name), *arguments],
        env=os.environ | {"LOCALAPPDATA": str(root)}, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Ollama" not in result.stdout
    args = json.loads((root / "args.json").read_text(encoding="utf-8-sig"))
    assert args[3] == expected[0]
    for flag, value in zip(expected[1::2], expected[2::2]):
        assert args[args.index(flag) + 1] == value


def test_launcher_rejects_invalid_account_suffix(tmp_path):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    script = Path(__file__).parents[2] / "scripts" / "agente-local.ps1"
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(script),
         "owner-account", "-Owner", "manuela", "-Suffix", "12a4"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0


@pytest.mark.parametrize("arguments", [["import", "-ReadOnly"], ["scan"], ["scheduled"]])
def test_only_commands_that_open_a_panel_accept_the_network(tmp_path, arguments):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    script = Path(__file__).parents[2] / "scripts" / "agente-local.ps1"
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(script), *arguments,
         "-Red", "-DataDir", str(tmp_path / "datos")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0 and "-Red solo aplica" in result.stderr
    assert not (tmp_path / "datos").exists()


def test_launcher_rejects_an_invalid_mailbox_name(tmp_path):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    script = Path(__file__).parents[2] / "scripts" / "agente-local.ps1"
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(script),
         "login", "-Mailbox", "..\fuera"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0


def test_launcher_signs_in_to_the_invoice_mailbox_with_its_own_log(tmp_path):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "settings.yaml").write_text("invoices: {}\n", encoding="utf-8")
    original = Path(__file__).parents[2] / "scripts" / "agente-local.ps1"
    (root / "scripts" / original.name).write_text(
        original.read_text(encoding="utf-8-sig").replace(".venv\\Scripts\\python.exe", ".venv\\Scripts\\python.ps1"),
        encoding="utf-8",
    )
    fake = root / ".venv" / "Scripts" / "python.ps1"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "$args | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'args.json')\n"
        "exit 0\n", encoding="utf-8",
    )
    data = tmp_path / "datos"
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
         "-File", str(root / "scripts" / original.name), "login", "-Mailbox", "facturas"],
        env=os.environ | {"LOCALAPPDATA": str(root), "AGENTE_LOCAL_DATA": str(data)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads((root / "args.json").read_text(encoding="utf-8-sig"))
    assert args[2] == "mail_invoices.main" and "--login" in args and "monthly_close.main" not in args
    log = Path(args[args.index("--log-file") + 1])
    assert log.parent == data / "MailInvoiceAgent" / "logs" and log.name.endswith("-login.log")


def run_copied_script(tmp_path, name, *arguments, local_data):
    """Run a copy of a script in a temporary project whose Python only records its arguments."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Requires Windows PowerShell.")
    root = tmp_path / "project"
    fake = root / ".venv" / "Scripts" / "python.ps1"
    fake.parent.mkdir(parents=True)
    # Not Set-Content: -WhatIf would suppress it.
    fake.write_text(
        "[IO.File]::WriteAllText((Join-Path $env:LOCALAPPDATA 'args.json'), ($args | ConvertTo-Json))\n"
        "exit 0\n", encoding="utf-8",
    )
    script = root / "scripts" / name
    script.parent.mkdir()
    original = Path(__file__).parents[2] / "scripts" / name
    script.write_text(
        original.read_text(encoding="utf-8-sig").replace(".venv\\Scripts\\python.exe", ".venv\\Scripts\\python.ps1"),
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(script), *arguments],
        env=os.environ | {"LOCALAPPDATA": str(root), "AGENTE_LOCAL_DATA": local_data},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads((root / "args.json").read_text(encoding="utf-8-sig"))
    return root, args[args.index("--data-dir") + 1], result.stdout


@pytest.mark.parametrize("name,arguments", [
    ("agente-local.ps1", ["serve"]), ("register-monthly-close.ps1", ["-WhatIf"]),
])
def test_scripts_keep_the_close_where_agente_local_data_says(tmp_path, name, arguments):
    data = tmp_path / "datos fuera"
    root, used, output = run_copied_script(tmp_path, name, *arguments, local_data=f" {data} ")
    assert Path(used) == data / "MonthlyCloseAgent"
    assert not (root / ".local").exists()
    if name.startswith("register"):
        assert str(data / "MonthlyCloseAgent") in output  # the task keeps the same folder


@pytest.mark.parametrize("name,arguments", [
    ("agente-local.ps1", ["serve"]), ("register-monthly-close.ps1", ["-WhatIf"]),
])
def test_scripts_default_to_the_project_folder_and_respect_an_explicit_one(tmp_path, name, arguments):
    root, used, _ = run_copied_script(tmp_path / "a", name, *arguments, local_data="")
    assert Path(used) == root / ".local" / "MonthlyCloseAgent"
    explicit = tmp_path / "explicita"
    _, used, _ = run_copied_script(
        tmp_path / "b", name, *arguments, "-DataDir", str(explicit), local_data=str(tmp_path / "otra"),
    )
    assert Path(used) == explicit
