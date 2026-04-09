---
name: lessons-learnt
description: Summarize recent development changes, decisions, bugs fixed, and lessons learned in this project
---

Review the recent changes to this project and produce a structured lessons-learned summary.

To do this:
1. Read git log or recent file modifications to identify what changed
2. Reconstruct the problem → root cause → fix arc for each significant change
3. Highlight gotchas, non-obvious decisions, and patterns worth remembering

Format the output as follows:

---

# Lessons Learnt — <date range>

## Infrastructure & Deployment

For each item:
**Problem:** what broke or was missing
**Root cause:** why it happened
**Fix:** what was changed
**Lesson:** the generalizable takeaway

## Application & Code

Same format.

## Security & Configuration

Same format.

---

Today's session summary (2026-04-06 → 2026-04-07):

## Infrastructure & Deployment

**Problem:** Pulumi `pulumi up` rebuilt the Docker image but did not redeploy ECS tasks.
**Root cause:** Task definitions referenced `image.imageName` (`:latest` tag), which never changes value between runs, so Pulumi saw no diff and skipped task definition replacement.
**Fix:** Switched to `image.repoDigest` (a `repo@sha256:…` digest that changes on every push).
**Lesson:** Always use content-addressed image references (`repoDigest`) in ECS task definitions when using Pulumi + ECR. Tag-based references (`:latest`) break the change-detection chain.

---

**Problem:** ECS tasks started then stopped — failing ELB health checks.
**Root cause:** The ALB health check path was `/api/health`, which requires an authenticated session and returns 401 for unauthenticated probes.
**Fix:** Added an unauthenticated `GET /health` endpoint; updated the target group health check path to `/health`.
**Lesson:** Health check endpoints must never require authentication. Keep them minimal — just return `{"status": "ok"}` with 200.

---

**Problem:** Dockerfile `CMD` used `--interface web` flag that `main.py` does not support; build context pointed to a non-existent `scripts/demo/` directory.
**Root cause:** Dockerfile and `index.ts` were written against an old directory layout that was never committed.
**Fix:** Created `server.py` as the container entry point (HTTP on port 8000, no TLS); updated build context to repo root; omitted `dockerfile` key so Docker defaults to `Dockerfile` in the context.
**Lesson:** Verify Dockerfile paths and CMD flags locally before committing. The container entry point for ECS should run plain HTTP — let the ALB/CloudFront handle TLS termination.

---

**Problem:** CloudFront → Auth0 callback URL mismatch error despite the correct URL being configured in Auth0.
**Root cause:** `APP_BASE_URL` was correctly injected as an env var, but Auth0 dashboard callback URL was configured for a different application (wrong Client ID).
**Fix:** Verified via `aws ecs describe-task-definition` that `APP_BASE_URL` and `AUTH0_CLIENT_ID` matched. User confirmed the Auth0 settings were saved to the correct application.
**Lesson:** When debugging OAuth callback mismatches, always cross-check the Client ID in the running container against the Auth0 application that holds the allowed callback URLs.

---

**Problem:** Pulumi `secretCfg()` helper had live credentials hardcoded as fallback defaults in `index.ts`, committed to source.
**Root cause:** Credentials were never migrated to `pulumi config set --secret`; the helper silently used hardcoded values.
**Fix:** Migrated all 6 secrets to Pulumi encrypted config; replaced `secretCfg()` with `cfg.requireSecret()` which fails fast if a secret is missing.
**Lesson:** Use `cfg.requireSecret()` from day one. `cfg.get()` with a hardcoded default is a security anti-pattern that silently leaks credentials into source control.

---

## Application & Code

**Problem:** Natural language → SQL translation produced inconsistent, hallucinated table names (`cost_analytics`, `cost_data`, `costs`, `cost_centers`) across repeated identical questions.
**Root cause (1):** `static_context()` returned an empty string when `lobs.json` was not found — the entire DuckDB schema context was never sent to the LLM.
**Root cause (2):** Schema context listed raw table names without column names or join examples, so the LLM guessed plausible-sounding names.
**Root cause (3):** `s3_usage` links to `application` via `application_id`, not directly to `cost_center`. Without an example, the LLM invented a non-existent direct join column.
**Fix (1):** Decoupled schema context from `lobs.json` — schema is now always appended regardless of file presence.
**Fix (2):** Added pre-built convenience views (`app_compute_cost`, `app_storage_cost`) in the MCP server that hide the attribution join complexity.
**Fix (3):** Rewrote `_DUCKDB_SCHEMA_CONTEXT` to lead with the views, include exact column names with types, and embed a complete working example query.
**Lesson:** LLMs generate SQL reliably only when given (a) exact table and column names, (b) a worked example for the most complex join pattern, and (c) an explicit list of table names that do NOT exist. Hiding join complexity behind views is more robust than teaching the LLM to write complex CTEs from scratch each time.

---

**Problem:** Redis connection failed with `[SSL] record layer failure` on every request.
**Root cause:** Redis Cloud uses a self-signed certificate. The `redis-py` client defaults to verifying the certificate, which fails.
**Fix:** Added `ssl_cert_reqs="none"` to the Redis client constructor.
**Lesson:** Cloud-managed Redis services (Redis Cloud, Azure Cache) typically use self-signed or private-CA certificates. Always set `ssl_cert_reqs="none"` unless you are pinning the CA cert explicitly.

---

**Problem:** `PyJWT not installed — JWT signature NOT verified (dev mode)` warning in production.
**Root cause:** `PyJWT` was missing from `requirements.txt`; `auth/oidc.py` fell back to decoding the JWT payload without signature verification.
**Fix:** Added `PyJWT[crypto]==2.10.1` to `requirements.txt`.
**Lesson:** The `[crypto]` extra is required for RS256 (RSA-based JWTs used by Auth0). Installing `PyJWT` without it will still fail at key loading time.

---

## NL-to-SQL Translation Tuning

The chatbot repeatedly generated wrong SQL for the same question across multiple attempts, with errors like `Table with name cost_analytics does not exist`. Here is the full sequence of what was tried and what actually worked.

### Iteration 1 — Add schema to system prompt (insufficient)

**What we tried:** Added `_DUCKDB_SCHEMA_CONTEXT` constant to `static_context()` listing table and column names.

**Why it failed:** `static_context()` contained this guard:
```python
try:
    lobs = json.load(open(KB_DIR / "lobs.json"))
except FileNotFoundError:
    return ""   # ← exited before appending schema context
```
`lobs.json` does not exist in the ECS container, so the LLM received an empty system prompt section and had zero schema knowledge. It fell back to plausible-sounding names (`cost_analytics`, `costs`, `cost_data`).

**Fix:** Decoupled schema context from `lobs.json`. Schema is always appended; lobs section is appended only when the file exists:
```python
def static_context(self) -> str:
    lines = []
    try:
        lobs = json.load(open(KB_DIR / "lobs.json"))
        # ... append lobs lines ...
    except FileNotFoundError:
        pass          # schema still appended below
    lines.append(_DUCKDB_SCHEMA_CONTEXT)
    return "\n".join(lines)
```

### Iteration 2 — Better schema description (insufficient)

**What we tried:** Added exact column names with types and a WARNING listing non-existent table names.

**Why it failed:** Even with exact table names, the LLM abbreviated column names (`cc.name` instead of `cc.cost_center_name`, `cc.id` instead of `cc.cost_center_id`) and tried to join `s3_usage` directly to `cost_center` — inventing a `cost_center_id` column that doesn't exist in that table. The `s3_usage → application → cost_center` two-hop join was not obvious from a flat schema listing.

### Iteration 3 — Example query in the prompt (partially helped)

**What we tried:** Added a complete worked example for "top 3 cost centers in March" using the raw attribution CTE pattern (joining `dremio_usage` + `snowflake_usage` through `user_app_access` → `application` → `cost_center`).

**Why it partially failed:** The example only covered compute cost. The LLM still didn't know how to handle `s3_usage`, which links via `application_id` rather than `user_id`, and continued hallucinating joins.

### Iteration 4 — Convenience views in the MCP server (worked)

**What we tried:** Created two pre-built DuckDB views in `mcp_server/server.py` at startup:

```sql
-- Compute costs with attribution already applied
CREATE VIEW app_compute_cost AS
WITH uac AS (
    SELECT user_id, COUNT(*) AS app_count FROM user_app_access GROUP BY user_id
)
SELECT ua.app_id, d.datetime, d.query_cost / uac.app_count AS cost, 'dremio' AS platform
FROM dremio_usage d JOIN uac USING (user_id) JOIN user_app_access ua USING (user_id)
UNION ALL
SELECT ua.app_id, s.datetime, s.query_cost / uac.app_count AS cost, 'snowflake' AS platform
FROM snowflake_usage s JOIN uac USING (user_id) JOIN user_app_access ua USING (user_id);

-- Storage costs normalised to same interface as compute
CREATE VIEW app_storage_cost AS
SELECT application_id AS app_id, datetime, storage_cost AS cost, s3_bucket, s3_folder
FROM s3_usage;
```

Restructured `_DUCKDB_SCHEMA_CONTEXT` to **lead with the views**, relegate raw tables to a secondary section, and embed a simple example using the views:

```
PREFERRED VIEWS for cost queries (use these — they handle attribution automatically):
  app_compute_cost(app_id, datetime, cost, platform)
  app_storage_cost(app_id, datetime, cost, s3_bucket, s3_folder)

REFERENCE TABLES (exact column names):
  cost_center(cost_center_id INT, cost_center_name TEXT, lob_id INT)
  application(app_id INT, app_name TEXT, cost_center_id INT)
  ...

NEVER use: cost_data, cost_analytics, costs, compute_costs

Example — top 3 cost centers in March 2026:
  SELECT cc.cost_center_name, ROUND(SUM(c.cost), 2) AS total_cost
  FROM (
      SELECT app_id, cost FROM app_compute_cost
      WHERE datetime >= '2026-03-01' AND datetime < '2026-04-01'
      UNION ALL
      SELECT app_id, cost FROM app_storage_cost
      WHERE datetime >= '2026-03-01' AND datetime < '2026-04-01'
  ) c
  JOIN application a ON c.app_id = a.app_id
  JOIN cost_center cc ON a.cost_center_id = cc.cost_center_id
  GROUP BY cc.cost_center_name ORDER BY total_cost DESC LIMIT 3;
```

**Why this worked:**
- The LLM no longer needs to reason about attribution logic — the views encapsulate it.
- Both views expose the same `(app_id, datetime, cost)` interface, so any cost-by-dimension query follows a single, learnable pattern.
- The example query is short enough to replicate closely and concrete enough to leave no ambiguity about column names.

### Key principles extracted

1. **Schema context must be unconditional.** Any `return ""` guard that can silently suppress the schema will cause the LLM to hallucinate. Validate at startup that context is non-empty.

2. **Lead with what to use, not what exists.** Listing raw tables first invites the LLM to query them directly. Show the high-level views first; raw tables second as "advanced use only".

3. **Hide join complexity behind views.** If a query requires a 3-table attribution CTE, create a view that pre-applies it. Asking an LLM to reconstruct a complex CTE from a schema description produces inconsistent results.

4. **Provide a verbatim example for the most common query pattern.** The LLM will copy it almost exactly. One correct example is worth a paragraph of instructions.

5. **Explicitly name tables that do NOT exist.** Listing `NEVER use: cost_data, cost_analytics, costs` in the schema context directly suppresses the most common hallucination patterns.

6. **Consistent column naming across views helps.** Normalising `storage_cost → cost` and `application_id → app_id` so all cost views have the same interface reduces the number of distinct patterns the LLM must learn.

---

## Architecture Decisions

**Decision:** Split MCP/DuckDB server into a separate ECS service with its own internal ALB, rather than running it as a sidecar.
**Rationale:** Independent scaling, independent deployments, cleaner failure isolation. The internal ALB (`internal: true`) is not internet-reachable; only the chatbot's security group can reach port 80.
**Gotcha:** Internal ALBs must be in private subnets. The security group ingress rule on the MCP ALB SG must reference the chatbot's *task* security group (not the chatbot ALB SG).

---

**Decision:** CloudFront in front of the chatbot ALB using `AllViewer` origin request policy + `CachingDisabled` cache policy.
**Rationale:** Provides HTTPS without managing certificates; `AllViewer` forwards the `Host` header and all query strings (required for Auth0 `?code=&state=` params) and session cookies.
**Gotcha:** `APP_BASE_URL` must be set to the CloudFront HTTPS URL (not the ALB URL) so `auth/oidc.py` builds the correct `redirect_uri` for Auth0. This is a Pulumi output, so it must be threaded through `pulumi.all([..., distribution.domainName])` into the task definition env.
