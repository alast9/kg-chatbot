---
name: snowflake-auth-setup
description: Reference for Snowflake + Entra ID SSO auth, SQL REST API vs MCP backend switching, and scalability notes
---

This skill is a reference document. Read it to understand the full auth wiring between
Entra ID (Azure AD) and Snowflake External OAuth, what was changed to make it work, and
when/how to switch execution backends.

---

## Backend selection — API vs MCP

Current default: **SQL REST API** (`SNOWFLAKE_BACKEND=api`).
Future option: **Snowflake native MCP** (`SNOWFLAKE_BACKEND=mcp`) — blocked on a Snowflake bug.

### Why we're on API now

Snowflake's native Cortex MCP server (`SYSTEM_EXECUTE_SQL` type) returns
`"Error parsing response"` for **all** queries — including `SELECT 1`.
Failures don't appear in `ACCOUNT_USAGE.QUERY_HISTORY`, meaning they fail inside
Snowflake's MCP protocol layer before reaching the query engine.

### Acid test to validate MCP is fixed

```bash
curl -X POST https://XJSKMFC-WQC92044.snowflakecomputing.com/api/v2/databases/DEMO_DB/schemas/PUBLIC/mcp-servers/DEMO_MCP_SERVER/sse \
  -H "Authorization: Bearer <entra_oauth_token>" \
  -H "X-Snowflake-Authorization-Token-Type: OAUTH" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"sql_exec","arguments":{"sql":"SELECT 1 AS n"}},"id":"test-1"}'
```
Expected (working): JSON with `result.content[0].text` containing `[{"N": "1"}]`
Actual (broken): `"Error parsing response"`

**Switch to MCP when all three pass:**
- [ ] `SELECT 1` returns a result row (not "Error parsing response")
- [ ] `SELECT CURRENT_TIMESTAMP()` returns a TIMESTAMP value (type serialization)
- [ ] Query appears in `ACCOUNT_USAGE.QUERY_HISTORY` (confirms engine is reached)

### How to switch

In Pulumi config or container env:
```bash
# Switch to MCP
pulumi config set SNOWFLAKE_BACKEND mcp -s azure-dev
# Optionally target a different MCP server
pulumi config set SNOWFLAKE_MCP_SERVER DEMO_MCP_SERVER_V2 -s azure-dev
```

Or at runtime: `SNOWFLAKE_BACKEND=mcp SNOWFLAKE_MCP_SERVER=DEMO_MCP_SERVER_V2 python main.py`

No code changes needed — `capabilities/snowflake.py` routes through `_exec_sql()` which
delegates to `_exec_via_api()` or `_exec_via_mcp()` based on the env var.

### MCP advantages (when fixed)

- No per-user token management (Snowflake handles auth natively in MCP SSE)
- Streaming result sets (no JSON payload size limit)
- Future Cortex tools (Analyst, Search) exposed automatically via `tools/list`
- Schema discovery could use `tools/list` instead of INFORMATION_SCHEMA query

### Schema scale breakpoint

| Table count | API behaviour | Recommendation |
|-------------|--------------|----------------|
| < 100 | Schema fits in context, LLM SQL quality good | Default — inject full schema |
| 100–200 | ~15–20k tokens; still workable | Add table comments for LLM guidance |
| 200–500 | Context bloat, LLM accuracy drops | Filter to curated table list in `_SCHEMA_QUERY` |
| 500+ | Exceeds practical context window | Two-stage: embed schema, retrieve top-K per query |

Current `DEMO_DB.PUBLIC` is well under 100 tables — no action needed yet.

---

## Architecture

```
User logs in via Entra ID (OIDC PKCE)
  → auth/oidc.py handle_callback() exchanges refresh_token for a Snowflake-scoped token
  → Token stored in UserSession.snowflake_token
  → Per WebSocket: token injected into SnowflakeCapability via set_snowflake_token()
  → SnowflakeCapability._run_sql() POSTs to Snowflake SQL REST API v2 with Bearer token
  → Snowflake validates via CHATBOT_ENTRA_EXTERNAL_OAUTH security integration
```

No separate Snowflake login. One Entra ID login gives access to everything.

---

## Snowflake SQL changes (run as ACCOUNTADMIN in Snowsight)

### 1. Create External OAuth security integration

```sql
CREATE OR REPLACE SECURITY INTEGRATION CHATBOT_ENTRA_EXTERNAL_OAUTH
  TYPE = EXTERNAL_OAUTH
  ENABLED = TRUE
  EXTERNAL_OAUTH_TYPE = AZURE
  EXTERNAL_OAUTH_ISSUER = 'https://sts.windows.net/7cb00da4-86c0-4bb9-812b-1c0bc56cc824/'
  EXTERNAL_OAUTH_JWS_KEYS_URL = 'https://login.microsoftonline.com/7cb00da4-86c0-4bb9-812b-1c0bc56cc824/discovery/v2.0/keys'
  EXTERNAL_OAUTH_AUDIENCE_LIST = ('api://5daaa11c-aff1-48ac-b265-d6fc645bc669')
  EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'upn'
  EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME'
  EXTERNAL_OAUTH_ANY_ROLE_MODE = 'ENABLE';
```

**Key gotchas:**
- `EXTERNAL_OAUTH_ISSUER` must be `https://sts.windows.net/<tenant-id>/` (with trailing slash).
  Entra ID access tokens (v1 endpoint) use this issuer, NOT `https://login.microsoftonline.com/.../v2.0`.
  If you see HTTP 401 code 390303, check the issuer first:
  ```sql
  ALTER SECURITY INTEGRATION CHATBOT_ENTRA_EXTERNAL_OAUTH
    SET EXTERNAL_OAUTH_ISSUER = 'https://sts.windows.net/7cb00da4-86c0-4bb9-812b-1c0bc56cc824/';
  ```
- `EXTERNAL_OAUTH_ANY_ROLE_MODE = 'ENABLE'` allows the token's `scp` claim to specify the role.
  Without this, Snowflake ignores the role in the token and falls back to the user's default role.

### 2. Create the DEMO_READER role and grant access

```sql
CREATE ROLE IF NOT EXISTS DEMO_READER;
GRANT USAGE ON DATABASE DEMO_DB TO ROLE DEMO_READER;
GRANT USAGE ON SCHEMA DEMO_DB.PUBLIC TO ROLE DEMO_READER;
GRANT SELECT ON ALL TABLES IN SCHEMA DEMO_DB.PUBLIC TO ROLE DEMO_READER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA DEMO_DB.PUBLIC TO ROLE DEMO_READER;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE DEMO_READER;
```

### 3. Map Entra ID users to Snowflake users

Each Entra ID user needs a matching Snowflake user. The `LOGIN_NAME` must match the `upn` claim
(i.e., the user's Entra ID UPN, typically their email):

```sql
-- Check existing users
SHOW USERS;

-- Create/update a user's LOGIN_NAME to match their Entra UPN
ALTER USER <snowflake_username> SET LOGIN_NAME = '<user@domain.com>';

-- Assign DEMO_READER as their default role
GRANT ROLE DEMO_READER TO USER <snowflake_username>;
ALTER USER <snowflake_username> SET DEFAULT_ROLE = DEMO_READER;
```

### 4. Verify the integration

```sql
DESCRIBE SECURITY INTEGRATION CHATBOT_ENTRA_EXTERNAL_OAUTH;
-- Check: EXTERNAL_OAUTH_ISSUER, EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'upn'
```

---

## Entra ID (Azure Portal) changes

### Snowflake app registration (app ID: 5daaa11c-aff1-48ac-b265-d6fc645bc669)

This is the Entra app that represents Snowflake as an OAuth resource. The chatbot requests
tokens scoped for this app to access Snowflake.

**1. Add the `session:role:DEMO_READER` scope** (via Graph API or Azure Portal):

In Azure Portal → App registrations → [Snowflake app] → Expose an API → Add a scope:
- Scope name: `session:role:DEMO_READER`
- Who can consent: Admins and users
- Admin consent display name: "Snowflake DEMO_READER role"

Or via Graph API:
```bash
# GET current scopes
curl -H "Authorization: Bearer $GRAPH_TOKEN" \
  "https://graph.microsoft.com/v1.0/applications/<object-id>/api"

# PATCH to add scope (merge with existing scopes, do NOT replace)
curl -X PATCH -H "Authorization: Bearer $GRAPH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"api": {"oauth2PermissionScopes": [<existing_scopes>, {"id": "<new-uuid>", "value": "session:role:DEMO_READER", "type": "User", "adminConsentDisplayName": "Snowflake DEMO_READER", "adminConsentDescription": "Snowflake DEMO_READER role", "isEnabled": true}]}}' \
  "https://graph.microsoft.com/v1.0/applications/<object-id>"
```

**2. Add `upn` optional claim** to access tokens:

Azure Portal → App registrations → [Snowflake app] → Token configuration → Add optional claim:
- Token type: Access
- Claim: `upn`

Or via Azure CLI:
```bash
az ad app update --id 5daaa11c-aff1-48ac-b265-d6fc645bc669 \
  --optional-claims '{"accessToken":[{"name":"upn","essential":true}]}'
```

Without `upn`, Snowflake cannot map the token to a user and returns HTTP 401 code 390303.

**3. Update the OAuth2PermissionGrant** (admin consent for chatbot to request Snowflake scopes):

```bash
# Find the grant
curl -H "Authorization: Bearer $GRAPH_TOKEN" \
  "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId eq '<chatbot-sp-object-id>'"

# PATCH to include the new scope (append to existing scope string)
curl -X PATCH -H "Authorization: Bearer $GRAPH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"scope": "session:role:DEMO_READER snowflake.query offline_access"}' \
  "https://graph.microsoft.com/v1.0/oauth2PermissionGrants/<grant-id>"
```

---

## Python code changes

### auth/oidc.py

**`SNOWFLAKE_ENTRA_SCOPE`** — must include `session:role:DEMO_READER` so the token's `scp`
claim carries the role name that Snowflake uses for role assignment:

```python
SNOWFLAKE_ENTRA_APP_ID = os.getenv(
    "SNOWFLAKE_ENTRA_APP_ID", "5daaa11c-aff1-48ac-b265-d6fc645bc669")
SNOWFLAKE_ENTRA_SCOPE  = f"api://{SNOWFLAKE_ENTRA_APP_ID}/session:role:DEMO_READER offline_access"
```

**`exchange_snowflake_token()`** — called at login time to silently exchange the user's
Entra refresh_token for a Snowflake-scoped access token. No separate login step needed.

**`handle_callback()`** — calls `exchange_snowflake_token()` immediately after verifying the
id_token. Stores result in `UserSession.snowflake_token` and `snowflake_token_expires_at`.

### capabilities/snowflake.py

Uses Snowflake SQL REST API v2 (`POST /api/v2/statements`) instead of the Snowflake native
MCP server. Reasons:
- Snowflake's native MCP server (`SYSTEM_EXECUTE_SQL` type) fails with "Error parsing response"
  for ALL queries (even `SELECT 1`) — failure happens before queries reach the query engine
  (nothing appears in ACCOUNT_USAGE.QUERY_HISTORY). Root cause: Snowflake MCP protocol bug.
- SQL REST API returns all column types as strings — no serialization issues.

Key constants:
```python
SNOWFLAKE_ACCOUNT   = "XJSKMFC-WQC92044"
SNOWFLAKE_DATABASE  = "DEMO_DB"
SNOWFLAKE_SCHEMA    = "PUBLIC"
SNOWFLAKE_WAREHOUSE = "COMPUTE_WH"
SNOWFLAKE_ROLE      = "DEMO_READER"
SQL_API_URL = f"https://{SNOWFLAKE_ACCOUNT}.snowflakecomputing.com/api/v2/statements"
```

Tool exposed to LLM: `snowflake_run_sql` (single tool, replaces the old MCP proxy tools).

Schema discovery: At login, `INFORMATION_SCHEMA.COLUMNS` is queried via SQL REST API and
cached per `user_id`. Injected into LLM system prompt via `dynamic_fragment()`.

Token refresh: `_token()` checks expiry and calls `exchange_snowflake_token()` if expired.

### interfaces/web.py

WebSocket handler injects per-user tokens on every connection:
```python
_inject_snowflake_user(core, user_session.user_id)          # for schema cache keying
_inject_snowflake_token(core,
    user_session.snowflake_token,
    user_session.snowflake_token_expires_at,
    user_session.refresh_token)                               # for SQL execution + refresh
```

---

## Error reference

| HTTP code | Snowflake code | Meaning | Fix |
|-----------|---------------|---------|-----|
| 401 | 390303 | Invalid OAuth access token | Check issuer, audience, upn claim |
| 400 | 390317 | Role not listed in access token | Add `session:role:DEMO_READER` to Entra scope |
| 400 | 391902 | Unsupported Accept header null | Add `"Accept": "application/json"` to request |
| MCP error | — | Error parsing response | Snowflake MCP bug; use SQL REST API instead |

---

## Tenant / app IDs (azure-dev)

| Item | Value |
|------|-------|
| Entra Tenant ID | `7cb00da4-86c0-4bb9-812b-1c0bc56cc824` |
| Snowflake Entra App ID | `5daaa11c-aff1-48ac-b265-d6fc645bc669` |
| Snowflake Account | `XJSKMFC-WQC92044` |
| Snowflake DB.Schema | `DEMO_DB.PUBLIC` |
| Snowflake Role | `DEMO_READER` |
| Snowflake Warehouse | `COMPUTE_WH` |
