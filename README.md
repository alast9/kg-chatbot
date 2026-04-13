# Knowledge Graph Chatbot — Azure AI Edition

---

## The Problem

Data analysts and business users in a financial services organisation routinely need
answers that span multiple systems — cloud cost data, customer records, and large-scale
transaction history. Today they have to:

- Log into three separate tools (a BI dashboard, a Snowflake SQL client, a Dremio
  query editor) with different credentials and interfaces
- Write or ask someone to write SQL for every ad-hoc question
- Manually join insights across systems to form a single narrative
- Wait for data engineering to fulfil requests that cross system boundaries

This creates a high friction loop between a business question and an answer, slows
decision-making, and locks insight behind technical gatekeepers.

---

## The Product

This is a **conversational AI data assistant** that gives authorised users a single
chat interface to ask questions across all three data platforms simultaneously —
without writing SQL, switching tools, or knowing which system holds the data.

**What a user can do:**

| Question type | Example | Where the answer comes from |
|---|---|---|
| Cost analytics | "What are the top 3 most expensive cost centres this quarter?" | Knowledge graph + DuckDB cost metrics |
| Trend analysis | "Give me a month-by-month breakdown of compute spend across all lines of business" | DuckDB (Jan–Mar 2026 usage data) |
| Entity lookup | "What does the Loan Origination System do and which cost centre owns it?" | Neo4j graph + Elasticsearch descriptions |
| Customer data | "How many customers do we have and who are the top spenders?" | Snowflake — DEMO_DB (100 customers) |
| Large-scale data | "Show me the top 3 revenue customers across our full customer base" | Dremio — 4.8M customers, 177M orders |
| Cross-system narrative | "Summarise our cost position and top customer revenue in one view" | All three systems in a single turn |

**Security is first-class.** Every user signs in with their corporate Azure AD
identity. That identity flows through to each data source — users only see data
their role permits. The assistant refuses to reveal credentials, internal
architecture, or data outside the user's access scope. A request for system
credentials is blocked by the Azure AI content filter before it ever reaches the
model.

**This is a demo product.** All data is synthetic. The purpose is to prove the
architecture pattern — a single authenticated AI layer over heterogeneous enterprise
data sources — and validate the guardrails, identity propagation, and query quality
before applying the pattern to real data.

---

## Components

### 1. Chatbot Container (FastAPI + WebSocket)

The entry point for all user interaction. A Python FastAPI application deployed as
an Azure Container App. It handles the SSO login flow, serves the browser UI, and
manages a persistent WebSocket connection per user session. Every user message
arrives over WebSocket; responses stream back token-by-token. The container has no
direct database credentials — it holds only the Entra ID client secret and the
Azure AI API key.

**Key files:** `server.py`, `interfaces/web.py`, `chatbot_core.py`

---

### 2. Identity — Microsoft Entra ID (Azure AD)

All authentication runs through Entra ID. The browser login uses `authorization_code
+ PKCE` (no client secret in the browser). After login the server holds an
`id_token` (who the user is) and an `access_token` (delegated identity to pass
downstream). Sessions are stored server-side behind an HttpOnly cookie — no tokens
are ever sent to the browser.

Two app registrations are provisioned by Pulumi:
- **KG Chatbot** — the web SSO application users log into
- **KG MCP Server** — a protected API resource; the chatbot holds the `MCP.Access`
  app role so it can call the MCP server machine-to-machine

---

### 3. AI Model — Claude on Azure AI

The chatbot calls Claude (Haiku 4.5) via an **Azure AI endpoint**, not the Anthropic
API directly. This keeps all model traffic within the Azure network perimeter and
allows the Azure AI content filter to intercept requests before they reach the model.
The content filter is the outermost guardrail — it blocked the credentials-exfiltration
test at HTTP 400 with 0 tokens consumed.

**Config:** `AZURE_AI_ENDPOINT`, `AZURE_AI_MODEL`, `AZURE_AI_API_KEY`

---

### 4. Knowledge Graph Capability

Three data stores that together answer cost analytics and entity lookup questions:

| Store | What it holds | How accessed |
|---|---|---|
| **Neo4j AuraDB** | Organisational hierarchy — LOBs, cost centres, applications and their relationships | Cypher queries via the Neo4j Python driver |
| **Elasticsearch** | Natural-language descriptions of every entity (RAG index) | `kg_describe_entity` / `kg_search_knowledge` tool calls |
| **DuckDB** | 100 K cost metric rows (Jan–Mar 2026) loaded from CSV files in Azure Blob Storage | SQL via an internal MCP server |

The DuckDB MCP server runs as a **separate Container App** (`mcp_server/`). It
validates every inbound request with a JWT signed by Entra ID — the chatbot obtains
that token using `client_credentials` (2LO, machine-to-machine). This means the MCP
server is not publicly callable; only the chatbot app role can reach it.

**Key files:** `capabilities/knowledge_graph.py`, `mcp_server/server.py`

---

### 5. Snowflake Capability

Snowflake queries run under the **user's own delegated identity**, not a service
account. The chatbot forwards the user's Entra ID `access_token` to the Azure AI
Foundry Agent Service, which exchanges it for a Snowflake OAuth token via a
WorkspaceConnection. The model never sees connection strings or passwords — it
calls tools (`snowflake_run_sql`, `snowflake_get_schema`) and gets back result rows.
Snowflake RBAC then enforces what tables that user's role can see.

**Key files:** `capabilities/snowflake.py`, `auth/azure_ai_gateway.py`

---

### 6. Dremio Capability

Structurally identical to Snowflake — user-delegated 3LO via the Azure AI Foundry
Agent Service. The gateway exchanges the Entra ID token for a Dremio Cloud OAuth
token. The capability exposes five tools (`dremio_nl_query`, `dremio_run_sql`,
`dremio_search_tables`, `dremio_get_lineage`, `dremio_system_tables`). The
`dremio_nl_query` tool adds a Bedrock-assisted NL→SQL step before executing, which
lets the model describe intent in English and have SQL generated automatically for
the Dremio schema.

**Key files:** `capabilities/dremio.py`, `auth/azure_ai_gateway.py`

---

### 7. Session Management

Two-tier persistence per user session:

- **MongoDB Atlas** (free tier) — full conversation history, persisted across
  reconnects. Every turn is written before the response is streamed back.
- **In-memory sliding window** — last 5 turns kept in process for fast context
  injection into the model prompt. Avoids sending the entire history on every turn.

**Key file:** `session.py`

---

### 8. Infrastructure — Pulumi (TypeScript)

All Azure resources are declared in `infrastructure-azure/index.ts` and managed by
Pulumi. A single `pulumi up` provisions the Container Apps environment, both
containers (chatbot + MCP server), ACR, Blob Storage, Log Analytics, Key Vault, AI
Foundry Hub, and both Entra ID app registrations including app roles, redirect URIs,
and test users. Secrets are Pulumi-encrypted (AES-256-GCM) and injected as Container
App secrets at deploy time — they never appear in source control.

The AWS ECS stack (`infrastructure/`) is kept intact but inactive, preserved for
future testing when AWS Bedrock AgentCore adds MCP 3LO support.

**Key files:** `infrastructure-azure/index.ts`, `infrastructure-azure/Pulumi.azure-dev.yaml`

---

## Architecture

```
Browser
  │ HTTPS (Azure-managed TLS)
  ▼
Chatbot Container App  ──────────────────────────────────────────────────────┐
  │  Entra ID SSO (authorization_code + PKCE)                                │
  │  Claude model via Azure AI endpoint  (NOT anthropic.com)                 │
  │                                                                           │
  ├── [KB / Cost Analytics]  2LO (client_credentials)                        │
  │       └─► KB MCP Server (internal Container App)                         │
  │               DuckDB ◄── Azure Blob Storage (CSV files)                  │
  │               JWT-validates each request (Entra ID JWKS)                 │
  │                                                                           │
  ├── [Snowflake]  3LO (user-delegated)                                      │
  │       └─► Azure AI Foundry Agent Service                                 │
  │               WorkspaceConnection: Snowflake OAuth 2.0                   │
  │               User identity flows to Snowflake MCP server                │
  │                                                                           │
  └── [Dremio]  3LO (user-delegated)                                        │
          └─► Azure AI Foundry Agent Service                                 │
                  WorkspaceConnection: Dremio MCP (via Dremio Cloud)        │
                  User identity flows to Dremio data layer                   │
                                                                             │
  Also queries: Neo4j (graph), Elasticsearch (descriptions)                  │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Auth Flows

### User login — Entra ID SSO (authorization_code + PKCE)

```
Browser → GET /auth/login
        → 302 to https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize
             ?client_id=<chatbot-app>&code_challenge=<S256>&scope=openid+profile+email+offline_access
Browser ← 302 /auth/callback?code=…&state=…
Chatbot → POST /oauth2/v2.0/token  (code + verifier + client_secret)
        ← id_token + access_token + refresh_token
        → verify id_token JWT (RS256, JWKS from Entra ID, aud=client_id)
        → create server-side session, set HttpOnly cookie
```

### KB MCP — 2LO machine-to-machine (client_credentials)

```
Chatbot → POST /oauth2/v2.0/token
             grant_type=client_credentials
             scope=api://kg-mcp-server-<stack>/.default
        ← access_token (aud=api://kg-mcp-server-<stack>)

Chatbot → POST http://kg-mcp-<stack>-app/query/sql
             Authorization: Bearer <token>
KB MCP  → validates JWT via Entra ID JWKS (aud, iss, expiry)
        ← SQL result
```

### Snowflake / Dremio — 3LO (user-delegated via Azure AI Agent)

```
Chatbot → Azure AI Foundry Agent Service  (user's Entra ID access_token)
Agent   → WorkspaceConnection (OAuth 2.0 / Snowflake)
        → exchanges Entra ID token for Snowflake OAuth token
        → calls Snowflake MCP server under the user's identity
        ← result → chatbot → user
```

---

## Repository Layout

```
knowledge_graph/
├── server.py                   Container entry point (port 8000)
├── chatbot_core.py             Stateless turn executor; calls Claude on Azure AI
├── session.py                  MongoDB full history + in-memory sliding window
│
├── capabilities/
│   ├── base.py                 Capability interface
│   ├── knowledge_graph.py      Neo4j + ES + DuckDB MCP (2LO auth to MCP server)
│   ├── dremio.py               Dremio via Azure AI Agent (3LO)
│   └── snowflake.py            Snowflake via Azure AI Agent (3LO)
│
├── auth/
│   ├── oidc.py                 Entra ID OIDC (authorization_code + PKCE)
│   ├── sso.py                  Entra ID M2M token manager (client_credentials)
│   ├── azure_ai_gateway.py     Azure AI Foundry Agent Service JSON-RPC client
│   └── agentcore_gateway.py    AWS AgentCore client (kept for AWS reference)
│
├── interfaces/
│   └── web.py                  FastAPI routes: auth, WebSocket chat, health
│
├── mcp_server/
│   ├── server.py               DuckDB HTTP server with JWT auth on /query/sql
│   ├── requirements.txt        fastapi, duckdb, PyJWT[crypto]
│   ├── Dockerfile
│   └── data/                   CSV files loaded into DuckDB views at startup
│
├── infrastructure/             AWS ECS Fargate stack (kept intact, not active)
├── infrastructure-azure/       Azure Container Apps stack (active production)
│   ├── index.ts                Pulumi program (Azure + Entra ID resources)
│   ├── Pulumi.azure-dev.yaml   Stack config (non-secret values + encrypted secrets)
│   └── package.json
│
├── Dockerfile                  Chatbot container (port 8000)
└── requirements.txt            Python dependencies
```

---

## Entra ID App Registrations

| App | Purpose | Grant type |
|-----|---------|-----------|
| `KG Chatbot` | Web SSO for users | authorization_code + PKCE |
| `KG MCP Server` | KB MCP server API (protected resource) | — (resource only) |

The chatbot app also holds the `MCP.Access` app role on the MCP server, which
enables `client_credentials` (2LO) from chatbot → KB MCP server.

### Users (created by Pulumi)

| UPN | Display name |
|-----|-------------|
| `alast9@<tenant>.onmicrosoft.com` | Alast 9 |
| `howard.wang.us1@<tenant>.onmicrosoft.com` | Howard Wang |

---

## Azure Resources (free-tier optimised)

| Resource | SKU | Cost |
|----------|-----|------|
| Container Apps Environment | Consumption | Free grant (180k vCPU-s/mo) |
| Azure Container Registry | Basic | ~$5/mo |
| Storage Account (LRS) | Standard | Free 12 mo / minimal |
| Log Analytics | PerGB2018, 0.16 GB/day cap | Free 5 GB/mo |
| Key Vault (AI Hub dependency) | Standard | Free 10k ops/mo |
| AI Foundry Hub + Project | Basic | Consumption |
| Entra ID | Free | Free 50k MAU |
| **Redis Cache** | **Removed** | **Saved ~$16/mo** |

Chat session history → MongoDB Atlas (free tier, external).
In-process session store → `auth/oidc.py:SessionStore` (in-memory dict).

---

## Environment Variables

### Chatbot container

| Variable | Description |
|----------|-------------|
| `ENTRA_TENANT_ID` | Azure AD tenant ID |
| `ENTRA_CLIENT_ID` | Chatbot app registration client ID |
| `ENTRA_CLIENT_SECRET` | Chatbot app client secret (Pulumi secret) |
| `ENTRA_MCP_API_URI` | KB MCP server identifier URI (`api://kg-mcp-server-<stack>`) |
| `ENTRA_AI_SCOPE` | Azure AI Foundry gateway scope (default: `https://cognitiveservices.azure.com/.default`) |
| `AZURE_AI_ENDPOINT` | Claude model endpoint on Azure AI |
| `AZURE_AI_MODEL` | Model name (e.g. `claude-haiku-4-5`) |
| `AZURE_AI_API_KEY` | Azure AI API key (Pulumi secret) |
| `AZURE_AI_FOUNDRY_GATEWAY_URL` | Azure AI Foundry Agent Service URL (for 3LO) |
| `APP_BASE_URL` | Public HTTPS URL of the chatbot (for Entra ID redirect URI) |
| `MCP_BASE` | Internal URL of the KB MCP server |
| `DREMIO_MCP_URL` | Dremio Cloud MCP endpoint |

### KB MCP server container

| Variable | Description |
|----------|-------------|
| `ENTRA_TENANT_ID` | Azure AD tenant ID (for JWKS endpoint) |
| `ENTRA_MCP_API_URI` | This server's identifier URI (for audience validation) |
| `DUCKDB_AZURE_CONTAINER` | Blob container name (`data`) |
| `AZURE_STORAGE_CONNECTION_STRING` | Storage account connection string (Pulumi secret) |

---

## Deployment

### Prerequisites

- Pulumi CLI + Azure credentials in `~/.bashrc` (`ARM_CLIENT_ID`, etc.)
- Docker running locally (for image builds)
- The Pulumi service principal needs **User Administrator** role in Entra ID
  to create users. Grant it once in:
  Azure Portal → Entra ID → Roles and administrators → User Administrator

### First deploy

```bash
cd infrastructure-azure
npm install
pulumi up -s azure-dev
```

### Post-deploy: configure Entra ID inbound OIDC on the AI Agent

The Azure AI Foundry Agent's inbound auth (for 3LO Snowflake/Dremio) is not
an ARM resource and must be configured once via the Azure AI Projects SDK:

```python
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

client = AIProjectClient(
    endpoint="<aiFoundryProjectEndpoint>",
    credential=DefaultAzureCredential(),
)
# Set Entra ID as inbound auth provider
client.agents.update_agent(
    agent_id="default",
    # discovery URL from: pulumi stack output entraTenantId
    oidc_discovery_url=(
        "https://login.microsoftonline.com/{tenantId}/v2.0"
        "/.well-known/openid-configuration"
    ),
    # from: pulumi stack output entraChatbotClientId
    allowed_clients=["<chatbotClientId>"],
)
```

### Subsequent deploys

```bash
cd infrastructure-azure
pulumi up -s azure-dev
```

Pulumi uses content-addressed image digests (`repoDigest`) so the container
app is always redeployed when the image changes.

### Useful stack outputs

```bash
pulumi stack output url                    # chatbot public URL
pulumi stack output entraTenantId          # Entra ID tenant ID
pulumi stack output entraChatbotClientId   # chatbot app client ID
pulumi stack output entraMcpApiUri         # KB MCP server identifier URI
pulumi stack output entraUser1Upn          # alast9@...
pulumi stack output entraUser2Upn          # howard.wang.us1@...
```

---

## Local Development

```bash
# Install Python deps
pip install -r requirements.txt

# Set minimal env vars for Entra ID (get from pulumi stack output)
export ENTRA_TENANT_ID=<tenantId>
export ENTRA_CLIENT_ID=<chatbotClientId>
export ENTRA_CLIENT_SECRET=<chatbotSecret>
export ENTRA_MCP_API_URI=api://kg-mcp-server-azure-dev

# Run chatbot (dev — no TLS, sessions in-memory)
APP_BASE_URL=http://localhost:8000 python server.py

# Run KB MCP server (separate terminal)
cd mcp_server
python server.py  # ENTRA_TENANT_ID not set → auth skipped (dev mode)
```

---

## Secrets Management

All secrets are stored as Pulumi encrypted config (AES-256-GCM):

```bash
# Required secrets (set once)
cd infrastructure-azure
pulumi config set --secret mongoUri          "<value>"   -s azure-dev
pulumi config set --secret neo4jPassword     "<value>"   -s azure-dev
pulumi config set --secret esKey             "<value>"   -s azure-dev
pulumi config set --secret azureAiApiKey     "<value>"   -s azure-dev
pulumi config set --secret snowflakeOAuthClientId     "<value>"   -s azure-dev
pulumi config set --secret snowflakeOAuthClientSecret "<value>"   -s azure-dev
```

The Entra ID client secret is auto-generated by Pulumi (`azuread.ApplicationPassword`)
and injected directly into the Container App secrets — it never appears in
`Pulumi.azure-dev.yaml` or source control.

---

## Testing

Full test documentation covering all four automated layers (unit, API, E2E, LLM evals)
and manual test execution results:

| Document | Description |
|----------|-------------|
| [TEST_PLAN.md](TEST_PLAN.md) | 25 executed test cases — all passed. Includes auth, KG, Snowflake, Dremio, guardrail, and UI results. |
| [TEST_DESIGN.md](TEST_DESIGN.md) | Full test design: pytest layers, fixtures, markers, TruLens evals, forbidden word list, and CI pipeline stages. |
| [chatbot_test_plan.pdf](chatbot_test_plan.pdf) | PDF version of the test plan for offline reference. |

**Test summary (April 10, 2026):** 25/25 passed · 6 guardrail tests passed · 0 failures
Notable: `TC-GR-03` (system credentials request) blocked by Azure AI content filter at HTTP 400 — 0 tokens, never reached the model.

---

## Two Infrastructure Stacks

| Stack | Location | Status | Notes |
|-------|----------|--------|-------|
| Azure | `infrastructure-azure/` | **Active** | Azure Container Apps, Entra ID |
| AWS   | `infrastructure/`        | Inactive | ECS Fargate — kept for future AWS 3LO testing |

The AWS stack is kept intact so it can be revived when AWS Bedrock AgentCore
adds MCP 2025-11-25 + 3LO support.
