"""session.py — Session management: MongoDB full history + Redis sliding window."""
from __future__ import annotations
import datetime
import json
import logging
import os

log = logging.getLogger("session")

MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB  = os.getenv("MONGO_DB",  "chatbot")
MONGO_COL = os.getenv("MONGO_COL", "conversations")

REDIS_HOST     = os.getenv("REDIS_HOST",     "localhost")
REDIS_PORT     = int(os.getenv("REDIS_PORT",   "6379"))
REDIS_SSL      = os.getenv("REDIS_SSL",      "false").lower() == "true"
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "") or None   # required for Azure Cache for Redis
REDIS_WINDOW   = 5   # Q&A pairs to keep in Redis


class MongoHistory:
    """Persist full conversation history to MongoDB Atlas."""

    def __init__(self):
        self._col = None
        if not MONGO_URI:
            log.warning("MONGO_URI not set — history in-memory only")
            return
        try:
            from pymongo import MongoClient
            c = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
            c.admin.command("ping")
            self._col = c[MONGO_DB][MONGO_COL]
            self._col.create_index("session_id")
            log.info("MongoDB connected (%s.%s)", MONGO_DB, MONGO_COL)
        except Exception as e:
            log.warning("MongoDB unavailable (%s) — history in-memory only", e)

    @property
    def ok(self) -> bool:
        return self._col is not None

    def load(self, session_id: str) -> list[dict]:
        """Return full message history for a session."""
        if not self.ok:
            return []
        try:
            doc = self._col.find_one({"session_id": session_id})
            return [{"role": m["role"], "content": m["content"]}
                    for m in (doc.get("messages", []) if doc else [])]
        except Exception as e:
            log.warning("MongoDB load error: %s", e)
            return []

    def append(self, session_id: str, role: str, content: str,
               capabilities: list[str] = None):
        """Append a single message to the session document."""
        if not self.ok:
            return
        now = datetime.datetime.utcnow().isoformat()
        try:
            self._col.update_one(
                {"session_id": session_id},
                {"$push": {"messages": {"role": role, "content": content, "ts": now}},
                 "$setOnInsert": {"session_id": session_id, "created_at": now,
                                  "capabilities": capabilities or []},
                 "$set": {"updated_at": now}},
                upsert=True)
        except Exception as e:
            log.warning("MongoDB append error: %s", e)

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List recent sessions with metadata."""
        if not self.ok:
            return []
        try:
            cursor = self._col.find(
                {}, {"session_id": 1, "created_at": 1, "updated_at": 1,
                     "capabilities": 1, "_id": 0}
            ).sort("updated_at", -1).limit(limit)
            return list(cursor)
        except Exception:
            return []

    def delete_session(self, session_id: str):
        if not self.ok:
            return
        try:
            self._col.delete_one({"session_id": session_id})
        except Exception:
            pass


class RedisWindow:
    """Keep the last REDIS_WINDOW Q&A pairs in Redis for fast retrieval."""

    _MAX = REDIS_WINDOW * 2   # 10 messages total

    def __init__(self):
        self._r = None
        try:
            import redis
            self._r = redis.Redis(
                host=REDIS_HOST, port=REDIS_PORT,
                ssl=REDIS_SSL, password=REDIS_PASSWORD,
                socket_connect_timeout=4, decode_responses=True)
            self._r.ping()
            log.info("Redis connected")
        except Exception as e:
            log.warning("Redis unavailable (%s) — recent window disabled", e)

    @property
    def ok(self) -> bool:
        return self._r is not None

    def _key(self, session_id: str) -> str:
        return f"chat:session:{session_id}"

    def push(self, session_id: str, role: str, content: str):
        if not self.ok:
            return
        try:
            k = self._key(session_id)
            p = self._r.pipeline()
            p.rpush(k, json.dumps({"role": role, "content": content}))
            p.ltrim(k, -self._MAX, -1)
            p.expire(k, 86400 * 7)
            p.execute()
        except Exception as e:
            log.warning("Redis push error: %s", e)

    def get_recent(self, session_id: str) -> list[dict]:
        if not self.ok:
            return []
        try:
            return [json.loads(m)
                    for m in self._r.lrange(self._key(session_id), 0, -1)]
        except Exception:
            return []

    def clear(self, session_id: str):
        if not self.ok:
            return
        try:
            self._r.delete(self._key(session_id))
        except Exception:
            pass


class SessionManager:
    """
    Manages a single chat session.
    Owns in-memory history (source of truth for the current process)
    and syncs to MongoDB + Redis.
    """

    def __init__(self, session_id: str, mongo: MongoHistory, redis: RedisWindow,
                 capabilities: list[str] = None):
        self.session_id   = session_id
        self._mongo       = mongo
        self._redis       = redis
        self._capabilities = capabilities or []
        # Load from MongoDB if resuming an existing session
        self._history: list[dict] = mongo.load(session_id)
        if self._history:
            log.info("Resumed session %s (%d messages)", session_id, len(self._history))

    @property
    def history(self) -> list[dict]:
        return list(self._history)

    @property
    def turn_count(self) -> int:
        return len(self._history) // 2

    def add_user(self, content: str):
        self._history.append({"role": "user", "content": content})
        self._mongo.append(self.session_id, "user", content, self._capabilities)
        self._redis.push(self.session_id, "user", content)

    def add_assistant(self, content: str):
        self._history.append({"role": "assistant", "content": content})
        self._mongo.append(self.session_id, "assistant", content)
        self._redis.push(self.session_id, "assistant", content)

    def get_recent(self) -> list[dict]:
        return self._redis.get_recent(self.session_id)

    def clear(self):
        """Clear in-memory + Redis (MongoDB history preserved for audit)."""
        self._history = []
        self._redis.clear(self.session_id)
