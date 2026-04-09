# Knowledge Graph Chatbot — Azure AI Edition

Multi-capability AI chatbot deployed on Azure Container Apps. Users sign in with
Entra ID (Azure AD). The chatbot calls Claude on Azure AI, and routes queries to
three data capabilities: a cost-analytics knowledge graph, Snowflake, and Dremio.
All MCP server calls are authenticated; user identity flows through to data sources.

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

## Two Infrastructure Stacks

| Stack | Location | Status | Notes |
|-------|----------|--------|-------|
| Azure | `infrastructure-azure/` | **Active** | Azure Container Apps, Entra ID |
| AWS   | `infrastructure/`        | Inactive | ECS Fargate — kept for future AWS 3LO testing |

The AWS stack is kept intact so it can be revived when AWS Bedrock AgentCore
adds MCP 2025-11-25 + 3LO support.
