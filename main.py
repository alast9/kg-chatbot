"""
main.py — Multi-Capability Chatbot (HTTPS + Entra ID SSO)
=========================================================
Web-only entry point. CLI removed in favour of SSO-protected web app.

Usage:
    python3 main.py                        # all capabilities, port 8443
    python3 main.py --caps dremio          # Dremio only
    python3 main.py --port 9443            # custom port
    python3 main.py --host 0.0.0.0         # listen on all interfaces

Entra ID app registration (Settings → your app):
  Allowed Callback URLs:   https://localhost:8443/auth/callback
  Allowed Logout URLs:     https://localhost:8443
  Allowed Web Origins:     https://localhost:8443

Required env vars: ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET, APP_BASE_URL

Then open: https://localhost:8443
  (Accept the self-signed certificate warning the first time)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

from capabilities import REGISTRY
from chatbot_core import ChatbotCore
from session import MongoHistory, RedisWindow


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Capability Chatbot (HTTPS + Entra ID SSO)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
After starting, open https://localhost:8443 in your browser.
Accept the self-signed certificate warning (one-time per browser).
Log in with your Entra ID credentials to access the chatbot.
        """
    )
    parser.add_argument("--caps",  default=",".join(REGISTRY),
                        help=f"Capabilities to load (default: all). Options: {list(REGISTRY)}")
    parser.add_argument("--port",  type=int, default=8443)
    parser.add_argument("--host",  default="127.0.0.1")
    parser.add_argument("--cert",  default=None, help="TLS cert .pem (auto-generated if omitted)")
    parser.add_argument("--key",   default=None, help="TLS key .pem (auto-generated if omitted)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Parse and instantiate capabilities ────────────────────────────────────
    requested = [c.strip() for c in args.caps.split(",") if c.strip()]
    unknown   = [c for c in requested if c not in REGISTRY]
    if unknown:
        print(f"Unknown capabilities: {unknown}. Available: {list(REGISTRY)}")
        sys.exit(1)

    print("\n── Capability Status ──────────────────────────────────────")
    active = []
    names  = []
    for name in requested:
        cap    = REGISTRY[name]()
        ok, msg = cap.startup_check()
        print(f"  {'✓' if ok else '✗'} {name}: {msg}")
        if ok:
            active.append(cap)
            names.append(name)

    if not active:
        print("\nNo capabilities available. Check connectivity.")
        sys.exit(1)

    print("────────────────────────────────────────────────────────────\n")

    # ── Shared backends ────────────────────────────────────────────────────────
    mongo = MongoHistory()
    redis = RedisWindow()
    core  = ChatbotCore(active)

    # ── Start HTTPS web server ─────────────────────────────────────────────────
    print(f"  Open: https://localhost:{args.port}")
    print(f"  (Accept self-signed certificate warning in browser)")
    print(f"  Entra ID callback: https://localhost:{args.port}/auth/callback\n")

    from interfaces.web import run as web_run
    web_run(core, mongo, redis, names,
            host=args.host, port=args.port,
            cert_file=args.cert, key_file=args.key)


if __name__ == "__main__":
    main()
