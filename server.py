"""Container entry point — HTTP on port 8000 (ALB handles TLS termination)."""
from __future__ import annotations
import logging
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")

from capabilities import REGISTRY
from chatbot_core import ChatbotCore
from session import MongoHistory, RedisWindow
from interfaces.web import create_app

active, names = [], []
for name, cls in REGISTRY.items():
    cap = cls()
    ok, msg = cap.startup_check()
    log.info("  %s %s: %s", "OK" if ok else "FAIL", name, msg)
    if ok:
        active.append(cap)
        names.append(name)

if not active:
    log.error("No capabilities available — exiting")
    raise SystemExit(1)

mongo = MongoHistory()
redis = RedisWindow()
core  = ChatbotCore(active)
app   = create_app(core, mongo, redis, names)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
