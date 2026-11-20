"""Opt-in access to a local panel from other devices on the home network.

Off unless a command asks for it (`-Red`). The panel then also listens, over HTTPS,
on this computer's private address. A device must be paired before it sees
anything: it asks for a one-time code, the code appears only in this computer's
terminal, and typing it gives the device a session cookie that expires. Paired
devices are kept per panel, by a hash of their session token, and can be listed
and revoked with `python -m shared.lan_access`.

The certificate comes from a local authority created once on this computer. It
is limited by name constraints to private IPv4 addresses, so installing it on a
phone cannot vouch for any public site.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import socketserver
import ssl
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from shared.storage import data_root

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
# Fixed so a single firewall rule (`lan-firewall` in the launcher) can allow them.
PANEL_PORTS = {"cierre": 8767, "facturas": 8768}
SESSION_SECONDS = 12 * 3600
CODE_SECONDS = 5 * 60
# Failed codes allowed per run; then pairing stays closed until the panel restarts.
MAX_FAILURES = 5
SOCKET_TIMEOUT = 30
CA_NAME = "agente-local red de casa"


class LanError(ValueError):
    """The panel cannot be opened to the network, or a pairing step was refused."""


def lan_folder() -> Path:
    return data_root() / "red-local"


def private_address() -> str:
    """This computer's address on the home network; refuses anything but a private IPv4.

    Connecting a UDP socket only chooses the outgoing interface: no packet is sent.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed to a real host
            address = probe.getsockname()[0]
        except OSError as error:
            raise LanError(f"No se encontró la dirección de la red local: {error}") from error
    return require_private(address)


def require_private(address: str) -> str:
    """The address itself when it belongs to a private home network (RFC 1918)."""
    if not any(ipaddress.ip_address(address) in net for net in PRIVATE_NETWORKS):
        raise LanError(f"La dirección {address} no es de una red privada; no se abre el panel a la red.")
    return address


# ------------------------------------------------------------------ certificates


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )


def ensure_authority(folder: Path) -> x509.Certificate:
    """The local certificate authority, created on first use; its certificate is ca.crt."""
    cert_path, key_path = folder / "ca.crt", folder / "ca.key"
    if cert_path.is_file() and key_path.is_file():
        return x509.load_pem_x509_certificate(cert_path.read_bytes())
    folder.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=5 * 365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        # Only private IPv4 addresses; the reserved .invalid name rules out every DNS name.
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName("agente-local.invalid")]
                + [x509.IPAddress(net) for net in PRIVATE_NETWORKS],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(key_path, _key_pem(key))
    _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    return cert


def issue_server_certificate(folder: Path, address: str) -> tuple[Path, Path]:
    """A fresh 30-day certificate for this address, signed by the local authority."""
    authority = ensure_authority(folder)
    ca_key = serialization.load_pem_private_key((folder / "ca.key").read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "agente-local")]))
        .issuer_name(authority.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()), critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path = folder / "servidor.crt", folder / "servidor.key"
    _write_private(key_path, _key_pem(key))
    _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 fingerprint, as phones show it, to check the authority installed is this one."""
    return ":".join(f"{byte:02X}" for byte in cert.fingerprint(hashes.SHA256()))


# ------------------------------------------------------------------ devices


def _now() -> float:
    return time.time()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_device_name(value: object) -> str:
    if not isinstance(value, str):
        raise LanError("Nombre de dispositivo inválido.")
    name = " ".join(value.split())
    if not 1 <= len(name) <= 40 or not name.isprintable():
        raise LanError("Escribe un nombre de dispositivo de 1 a 40 caracteres.")
    return name


@dataclass(frozen=True)
class Device:
    name: str
    token_sha256: str
    paired_at: float
    expires_at: float


class DeviceRegistry:
    """Paired devices of one panel, in a JSON file; only token hashes are stored.

    The file is read on every check, so a revocation from another terminal applies
    at once.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = _now) -> None:
        self.path, self._clock = path, clock
        self._lock = threading.Lock()

    def devices(self) -> list[Device]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as error:
            raise LanError(f"No se pudo leer {self.path.name}: {error}") from error
        return [Device(**item) for item in raw.get("devices", [])]

    def _save(self, devices: list[Device]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"devices": [vars(d) for d in devices]}, ensure_ascii=False, indent=2)
        _write_private(self.path, data.encode("utf-8"))

    def active(self) -> list[Device]:
        now = self._clock()
        return [device for device in self.devices() if device.expires_at > now]

    def device_for(self, token: str) -> str | None:
        """The name of the device holding this session token, while it has not expired."""
        if not token:
            return None
        digest = _token_hash(token)
        for device in self.active():
            if secrets.compare_digest(device.token_sha256, digest):
                return device.name
        return None

    def pair(self, name: str) -> str:
        """Register a device (replacing one with the same name) and return its session token."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            kept = [device for device in self.active() if device.name != name]
            self._save(kept + [Device(name, _token_hash(token), now, now + SESSION_SECONDS)])
        return token

    def revoke(self, name: str | None) -> int:
        """Remove one device by name, or every device when name is None; returns how many."""
        with self._lock:
            devices = self.devices()
            kept = [device for device in devices if name is not None and device.name != name]
            if len(kept) != len(devices):
                self._save(kept)
            return len(devices) - len(kept)


class Pairing:
    """One-time codes, shown only in this computer's terminal, that pair a device."""

    def __init__(self, announce: Callable[[str], None], clock: Callable[[], float] = _now) -> None:
        self._announce, self._clock = announce, clock
        self._lock = threading.Lock()
        self._pending: tuple[str, str, str, float] | None = None  # name, client, code, expires
        self.failures = 0

    def request(self, name: str, client: str, panel: str) -> None:
        with self._lock:
            self._check_open()
            now = self._clock()
            if self._pending and self._pending[3] > now and self._pending[1] != client:
                raise LanError("Otro dispositivo está emparejándose; espera unos minutos.")
            code = f"{secrets.randbelow(10**8):08d}"
            self._pending = (name, client, code, now + CODE_SECONDS)
        self._announce(
            f"El dispositivo «{name}» ({client}) pide entrar al panel de {panel}. "
            f"Código: {code[:4]} {code[4:]} (vence en {CODE_SECONDS // 60} minutos). "
            "Si no lo pediste tú, no lo escribas en ningún lado."
        )

    def confirm(self, code: object, client: str) -> str:
        """The paired device's name for a correct code from the client that asked for it."""
        with self._lock:
            self._check_open()
            pending = self._pending
            typed = "".join(code.split()) if isinstance(code, str) else ""
            if (
                pending is None or pending[3] <= self._clock() or pending[1] != client
                or not secrets.compare_digest(typed.encode(), pending[2].encode())
            ):
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    self._pending = None
                    self._announce("Emparejamiento bloqueado por códigos fallidos; reinicia el panel para volver a intentarlo.")
                raise LanError("Código incorrecto o vencido.")
            self._pending = None
            return pending[0]

    def _check_open(self) -> None:
        if self.failures >= MAX_FAILURES:
            raise LanError("Emparejamiento bloqueado por códigos fallidos; reinicia el panel en el computador.")


# ------------------------------------------------------------------ server


class _TlsServer(ThreadingHTTPServer):
    """HTTPS server whose TLS handshake runs in each request's thread, with a timeout."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler, context: ssl.SSLContext, lan: LanAccess) -> None:
        self.context, self.lan_access = context, lan
        super().__init__(address, handler)

    def server_bind(self) -> None:
        # HTTPServer would look up the address's DNS name, which stalls for seconds on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def finish_request(self, request, client_address) -> None:
        request.settimeout(SOCKET_TIMEOUT)
        try:
            tls = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # a device that does not trust the certificate yet, or a port scan
        try:
            super().finish_request(tls, client_address)
        finally:
            tls.close()


class LanAccess:
    """Everything one panel needs to accept paired devices from the home network."""

    def __init__(
        self, panel: str, *, announce: Callable[[str], None] = print, folder: Path | None = None,
        address: str | None = None, port: int | None = None, clock: Callable[[], float] = _now,
    ) -> None:
        if panel not in PANEL_PORTS:
            raise LanError(f"Panel desconocido: {panel}.")
        self.panel = panel
        self.folder = folder or lan_folder()
        self.address = address or private_address()
        self.port = PANEL_PORTS[panel] if port is None else port
        self.cookie = f"__Host-sesion-{panel}"
        self.devices = DeviceRegistry(self.folder / f"dispositivos-{panel}.json", clock)
        self.pairing = Pairing(announce, clock)
        self._announce = announce
        self.authority = ensure_authority(self.folder)

    def serve(self, handler) -> ThreadingHTTPServer:
        """Bind the HTTPS server for this panel's handler; the caller runs serve_forever."""
        cert, key = issue_server_certificate(self.folder, self.address)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server = _TlsServer((self.address, self.port), handler, context, self)
        self.port = server.server_port
        self._announce(
            f"Panel en la red local: https://{self.address}:{self.port}/ (solo dispositivos emparejados). "
            f"Huella del certificado de la red de casa: {fingerprint(self.authority)}"
        )
        return server

    @property
    def authority_pem(self) -> bytes:
        return self.authority.public_bytes(serialization.Encoding.PEM)

    def session_cookie(self, token: str) -> str:
        return f"{self.cookie}={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict"


def start_in_background(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    return thread


# ------------------------------------------------------------------ command line


def main(argv: list[str] | None = None) -> int:
    """List or revoke the devices paired with each panel."""
    parser = argparse.ArgumentParser(description="Dispositivos emparejados con los paneles en la red local.")
    parser.add_argument("--revoke", metavar="NOMBRE", help="Revoca este dispositivo en los dos paneles.")
    parser.add_argument("--revoke-all", action="store_true", help="Revoca todos los dispositivos.")
    args = parser.parse_args(argv)
    try:
        for panel in PANEL_PORTS:
            registry = DeviceRegistry(lan_folder() / f"dispositivos-{panel}.json")
            if args.revoke_all or args.revoke:
                removed = registry.revoke(None if args.revoke_all else args.revoke)
                print(f"Panel de {panel}: {removed} dispositivo(s) revocado(s).")
                continue
            active = registry.active()
            print(f"Panel de {panel}: {len(active) or 'ningún'} dispositivo(s) con sesión vigente.")
            for device in active:
                until = datetime.fromtimestamp(device.expires_at).strftime("%Y-%m-%d %H:%M")
                print(f"  - {device.name} (vence {until})")
    except LanError as error:
        print(f"Detenido: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
