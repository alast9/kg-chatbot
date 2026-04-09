"""
mcp_server/server.py — DuckDB cost-analytics HTTP server.

Endpoints
---------
GET  /health        liveness probe (no auth required)
POST /query/sql     execute a read-only SQL SELECT via DuckDB
                    requires Entra ID Bearer token (2LO — chatbot app only)

Auth (2LO machine-to-machine):
  The chatbot obtains a client_credentials token scoped to this server's
  identifier URI (ENTRA_MCP_API_URI) and sends it as:
    Authorization: Bearer <token>
  This server validates the token via the Entra ID JWKS endpoint:
    audience == ENTRA_MCP_API_URI
    issuer   == https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0
  If ENTRA_TENANT_ID or ENTRA_MCP_API_URI is unset, auth is skipped with
  a warning (dev / standalone mode).

Configuration (env vars) — set one storage backend:
------------------------
Azure Blob Storage (preferred on Azure):
  DUCKDB_AZURE_CONTAINER           Azure Blob container name holding CSV files
  AZURE_STORAGE_CONNECTION_STRING  Azure Storage account connection string

AWS S3 (fallback):
  DUCKDB_S3_BUCKET   S3 bucket that holds the CSV data files
  AWS_REGION         AWS region for S3 (default: us-east-1)

Auth env vars:
  ENTRA_TENANT_ID    Azure AD tenant ID
  ENTRA_MCP_API_URI  Identifier URI of this server's app registration
                     e.g. api://kg-mcp-server-azure-dev

PORT               listen port (default: 8001)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.request

import duckdb
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_server")

# ── Entra ID JWT validation config ───────────────────────────────────────────
ENTRA_TENANT_ID   = os.getenv("ENTRA_TENANT_ID",   "")
ENTRA_MCP_API_URI = os.getenv("ENTRA_MCP_API_URI", "")
_AUTH_ENABLED     = bool(ENTRA_TENANT_ID and ENTRA_MCP_API_URI)

if not _AUTH_ENABLED:
    log.warning("ENTRA_TENANT_ID or ENTRA_MCP_API_URI not set — "
                "token validation DISABLED (dev mode only)")

ENTRA_JWKS_URL  = (f"https://login.microsoftonline.com"
                   f"/{ENTRA_TENANT_ID}/discovery/v2.0/keys"
                   if ENTRA_TENANT_ID else "")
ENTRA_ISSUER    = (f"https://login.microsoftonline.com"
                   f"/{ENTRA_TENANT_ID}/v2.0"
                   if ENTRA_TENANT_ID else "")


class _JWKSCache:
    """Simple thread-safe JWKS key cache for Entra ID JWT verification."""

    def __init__(self):
        self._keys: dict[str, dict] = {}
        self._fetched_at = 0.0
        self._ttl  = 3600
        self._lock = threading.Lock()

    def get_key(self, kid: str) -> dict | None:
        with self._lock:
            if time.time() - self._fetched_at > self._ttl or not self._keys:
                self._refresh()
            return self._keys.get(kid)

    def _refresh(self):
        if not ENTRA_JWKS_URL:
            return
        try:
            with urllib.request.urlopen(ENTRA_JWKS_URL, timeout=10) as r:
                data = json.loads(r.read())
            self._keys = {k["kid"]: k for k in data.get("keys", [])}
            self._fetched_at = time.time()
        except Exception as e:
            log.warning("JWKS refresh failed: %s", e)


_jwks_cache = _JWKSCache()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _validate_bearer_token(authorization: str | None) -> None:
    """
    Validate Entra ID Bearer token on the /query/sql endpoint.
    Raises HTTPException(401) if invalid; does nothing if auth is disabled.
    """
    if not _AUTH_ENABLED:
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401,
                            detail="Authorization: Bearer <token> required")
    token = authorization[len("Bearer "):]

    try:
        import jwt as pyjwt
        from jwt.algorithms import RSAAlgorithm

        header  = json.loads(_b64url_decode(token.split(".")[0]))
        jwk     = _jwks_cache.get_key(header.get("kid", ""))
        if not jwk:
            raise ValueError("Unknown JWT kid")

        pub_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        pyjwt.decode(
            token, pub_key,
            algorithms=["RS256"],
            audience=ENTRA_MCP_API_URI,
            issuer=ENTRA_ISSUER,
        )
        log.debug("Token validated OK")

    except ImportError:
        # PyJWT not installed — do manual expiry check only
        log.warning("PyJWT not installed — only checking token expiry (dev mode)")
        payload = json.loads(_b64url_decode(token.split(".")[1]))
        if payload.get("exp", 0) < time.time():
            raise HTTPException(status_code=401, detail="Token expired")
        if payload.get("aud") not in (ENTRA_MCP_API_URI, [ENTRA_MCP_API_URI]):
            raise HTTPException(status_code=401, detail="Invalid token audience")

    except Exception as e:
        log.warning("Token validation failed: %s", e)
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


# Azure Blob Storage (takes priority)
AZURE_CONTAINER    = os.getenv("DUCKDB_AZURE_CONTAINER", "")
AZURE_CONN_STR     = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")

# AWS S3 (fallback)
S3_BUCKET  = os.getenv("DUCKDB_S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

PORT = int(os.getenv("PORT", "8001"))

# Tables that must exist as <table>.csv in the S3 bucket root
TABLES = [
    "lob",
    "cost_center",
    "application",
    "users",
    "user_app_access",
    "dremio_usage",
    "snowflake_usage",
    "s3_usage",
]

# ── DuckDB connection + view setup ────────────────────────────────────────────

conn = duckdb.connect(":memory:")

# ── Convenience views (shared between storage backends) ───────────────────────

def _create_convenience_views():
    conn.execute("""
        CREATE OR REPLACE VIEW app_compute_cost AS
        WITH uac AS (
            SELECT user_id, COUNT(*) AS app_count
            FROM user_app_access GROUP BY user_id
        )
        SELECT ua.app_id,
               d.datetime,
               d.query_cost / uac.app_count AS cost,
               'dremio' AS platform
        FROM dremio_usage d
        JOIN uac USING (user_id)
        JOIN user_app_access ua USING (user_id)
        UNION ALL
        SELECT ua.app_id,
               s.datetime,
               s.query_cost / uac.app_count AS cost,
               'snowflake' AS platform
        FROM snowflake_usage s
        JOIN uac USING (user_id)
        JOIN user_app_access ua USING (user_id);
    """)
    conn.execute("""
        CREATE OR REPLACE VIEW app_storage_cost AS
        SELECT application_id AS app_id,
               datetime,
               storage_cost    AS cost,
               s3_bucket,
               s3_folder
        FROM s3_usage;
    """)


if AZURE_CONTAINER and AZURE_CONN_STR:
    # ── Azure Blob Storage backend ─────────────────────────────────────────────
    # Download CSVs via azure-storage-blob SDK (uses Python SSL — no DuckDB
    # extension SSL cert issues) then load into DuckDB from local temp files.
    try:
        import tempfile
        from azure.storage.blob import BlobServiceClient

        tmp_dir = tempfile.mkdtemp(prefix="mcp_csv_")
        svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
        container_client = svc.get_container_client(AZURE_CONTAINER)

        for table in TABLES:
            blob_name = f"{table}.csv"
            local_path = os.path.join(tmp_dir, blob_name)
            blob_client = container_client.get_blob_client(blob_name)
            with open(local_path, "wb") as f:
                f.write(blob_client.download_blob().readall())
            conn.execute(
                f"CREATE OR REPLACE VIEW {table} AS "
                f"SELECT * FROM read_csv_auto('{local_path}', header=true);"
            )
            log.info("View created: %s → %s", table, local_path)

        _create_convenience_views()
        log.info("All views loaded from Azure Blob via SDK (container: %s)", AZURE_CONTAINER)
    except Exception as e:
        log.error("Failed to set up Azure Blob views: %s", e)
        raise SystemExit(1)

elif S3_BUCKET:
    # ── AWS S3 backend ────────────────────────────────────────────────────────
    try:
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        conn.execute(f"""
            CREATE OR REPLACE SECRET aws_creds (
                TYPE S3,
                PROVIDER credential_chain,
                REGION '{AWS_REGION}'
            );
        """)
        for table in TABLES:
            s3_path = f"s3://{S3_BUCKET}/{table}.csv"
            conn.execute(
                f"CREATE OR REPLACE VIEW {table} AS "
                f"SELECT * FROM read_csv_auto('{s3_path}', header=true);"
            )
            log.info("View created: %s → %s", table, s3_path)
        _create_convenience_views()
        log.info("All views loaded from s3://%s", S3_BUCKET)
    except Exception as e:
        log.error("Failed to set up S3 views: %s", e)
        raise SystemExit(1)

else:
    log.warning("No storage backend configured — starting with empty in-memory DB")

# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Cost Analytics MCP Server", version="1.0.0")


class SqlRequest(BaseModel):
    sql: str
    description: str = ""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/query/sql")
def query_sql(req: SqlRequest, request: Request):
    _validate_bearer_token(request.headers.get("authorization"))

    first_word = req.sql.strip().lstrip("(").split()[0].upper()
    if first_word not in ("SELECT", "WITH"):
        raise HTTPException(status_code=400, detail="Only SELECT statements are permitted")

    log.info("SQL [%s]: %.120s", req.description[:50], req.sql)
    try:
        rel     = conn.execute(req.sql)
        columns = [d[0] for d in rel.description]
        rows    = rel.fetchall()
        return JSONResponse({
            "columns":   columns,
            "rows":      json.loads(json.dumps(rows, default=str)),
            "row_count": len(rows),
        })
    except Exception as e:
        log.error("SQL error (%.120s): %s", req.sql, e)
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
