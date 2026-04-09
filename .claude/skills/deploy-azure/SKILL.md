---
name: deploy-azure
description: Deploy or redeploy the KG Chatbot to Azure, check stack health, and surface any config or auth issues before running pulumi up
---

You are helping deploy the KG Chatbot to the Azure Container Apps stack
(`infrastructure-azure/`, stack `azure-dev`).

Carry out the following steps in order.  Stop and report clearly if any step fails.

---

## Step 1 — Pre-flight checks

Run these checks **before** touching Pulumi:

1. Verify Azure credentials are set:
   ```
   echo $ARM_CLIENT_ID $ARM_TENANT_ID $ARM_SUBSCRIPTION_ID
   ```
   If any are blank, tell the user to run `source ~/.bashrc` or set them.

2. Verify Pulumi is logged in:
   ```
   pulumi whoami
   ```

3. Check required Pulumi secrets are present (none should show as `[secret]` missing):
   ```
   cd infrastructure-azure && pulumi config -s azure-dev --show-secrets 2>&1 | grep -E "mongoUri|neo4jPassword|esKey|azureAiApiKey|snowflakeOAuth"
   ```
   If any secret is missing, tell the user the exact `pulumi config set --secret` command to run.

4. Verify Docker is running:
   ```
   docker info --format '{{.ServerVersion}}' 2>&1 | head -1
   ```

5. Check that all CSV data files exist (needed for Blob upload):
   ```
   ls knowledge_graph/mcp_server/data/*.csv 2>/dev/null | wc -l
   ```
   There should be 8 files. If not, warn the user.

---

## Step 2 — TypeScript type-check

```bash
cd infrastructure-azure
node --max-old-space-size=4096 ./node_modules/.bin/tsc --noEmit 2>&1
```

If there are errors, read `infrastructure-azure/index.ts` to diagnose and fix them
before proceeding.

---

## Step 3 — Preview the changes

```bash
cd infrastructure-azure && pulumi preview -s azure-dev 2>&1
```

Summarise what Pulumi intends to create / update / delete.  Flag anything
unexpected (e.g. resource replacement that would cause downtime, deletion of
a user or app registration).

---

## Step 4 — Deploy

```bash
cd infrastructure-azure && pulumi up -s azure-dev --yes 2>&1
```

Tail the output. If the deployment fails:
- Read the full error message.
- Check for common causes listed in Step 5.
- Attempt a targeted fix if the cause is clear.
- Otherwise report the error and stack trace to the user.

---

## Step 5 — Post-deploy validation

After `pulumi up` succeeds:

1. Retrieve the chatbot URL:
   ```
   pulumi stack output url -s azure-dev
   ```

2. Health-check the chatbot:
   ```
   curl -sf <url>/health
   ```
   Expect `{"status":"ok"}`.

3. Retrieve Entra ID outputs for reference:
   ```
   pulumi stack output entraTenantId          -s azure-dev
   pulumi stack output entraChatbotClientId   -s azure-dev
   pulumi stack output entraMcpApiUri         -s azure-dev
   pulumi stack output entraUser1Upn          -s azure-dev
   pulumi stack output entraUser2Upn          -s azure-dev
   ```

4. Remind the user about the one-time post-deploy step:
   > **Reminder:** Configure Entra ID inbound OIDC on the Azure AI Agent via
   > the Azure AI Projects SDK (see README.md → Post-deploy section).
   > This is required for Snowflake and Dremio 3LO to work.

---

## Common failure causes and fixes

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `User Administrator` permission error when creating Entra ID users | Pulumi SP lacks the role | Azure Portal → Entra ID → Roles → User Administrator → grant to SP |
| `identifierUris already in use` | Another app registration already has `api://kg-mcp-server-azure-dev` | Delete the stale app in Entra ID or change the stack name |
| Docker push fails with `unauthorized` | ACR credentials stale | Re-run `pulumi up`; Pulumi fetches fresh ACR creds each run |
| Container App health check fails | Image build error or missing env var | Check Container App logs: `az containerapp logs show -n <name> -g <rg>` |
| Entra ID `access_denied` on login | Redirect URI not registered | Confirm `<chatbotBaseUrl>/auth/callback` is in the app registration |
| KB MCP returns 401 | Token audience mismatch | Confirm `ENTRA_MCP_API_URI` matches the app registration's identifier URI |
| JWT `Unknown kid` in MCP server | JWKS cache stale | Restart the MCP container app |
| AI Hub creation times out | Provisioning can take 30–60 min | Wait and re-run `pulumi up`; Hub creation is idempotent |

---

## Auth0 is no longer used

This project migrated from Auth0 to Entra ID.  If you see any `AUTH0_` env vars
or references to `dev-17z0ihexvjnnml4s.us.auth0.com`, they are stale and should
be removed.  Do not add Auth0 configuration.
