"""
auth/tls.py — Self-signed TLS certificate management
=====================================================
Generates a self-signed certificate for localhost HTTPS if one doesn't
already exist. Certificate is valid for localhost and 127.0.0.1 and
expires in 825 days (the maximum Chrome trusts for self-signed certs).

Usage:
    from auth.tls import ensure_certs, CERT_FILE, KEY_FILE
    ensure_certs()   # idempotent — generates only if missing or expired

Browser trust:
    The first time you visit https://localhost:8443 your browser will show
    a "Your connection is not private" warning. This is expected for
    self-signed certs. Click "Advanced" → "Proceed to localhost (unsafe)".
    The warning does not appear again for the same cert.

    Chrome/Edge: type "thisisunsafe" on the warning page to bypass.
    Firefox:     Click "Advanced" → "Accept the Risk and Continue".
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("auth.tls")

CERTS_DIR = Path(os.getenv("CERTS_DIR", Path(__file__).parent.parent / "certs"))
CERT_FILE = CERTS_DIR / "cert.pem"
KEY_FILE  = CERTS_DIR / "key.pem"


def _cert_valid() -> bool:
    """Return True if cert exists and is not expired."""
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        return False
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(CERT_FILE), "-noout", "-checkend", "86400"],
            capture_output=True)
        # exit 0 = cert valid for at least 1 more day
        return result.returncode == 0
    except Exception:
        return False


def generate_cert() -> None:
    """Generate a self-signed cert valid for localhost + 127.0.0.1."""
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Generating self-signed TLS certificate in %s", CERTS_DIR)
    result = subprocess.run([
        "openssl", "req",
        "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(KEY_FILE),
        "-out",    str(CERT_FILE),
        "-days",   "825",
        "-subj",   "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"openssl failed:\n{result.stderr}")

    # Restrict key file permissions
    KEY_FILE.chmod(0o600)
    log.info("TLS certificate generated: %s (expires ~825 days)", CERT_FILE)


def ensure_certs() -> tuple[str, str]:
    """
    Ensure valid TLS cert and key exist. Generate if missing or expiring.
    Returns (cert_path, key_path) as strings for uvicorn.
    """
    if not _cert_valid():
        generate_cert()
    else:
        log.info("Using existing TLS certificate: %s", CERT_FILE)
    return str(CERT_FILE), str(KEY_FILE)
