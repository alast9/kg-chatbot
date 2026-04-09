import * as pulumi from "@pulumi/pulumi";
import * as azure_native from "@pulumi/azure-native";
import * as mlv2025 from "@pulumi/azure-native/machinelearningservices/v20250101preview";
import * as azuread from "@pulumi/azuread";
import * as command from "@pulumi/command";
import * as docker from "@pulumi/docker";
import * as path from "path";

const stack = pulumi.getStack();
const cfg   = new pulumi.Config();

// Naming helpers
const n  = (r: string) => `kg-chatbot-${stack}-${r}`;   // chatbot resources
const mn = (r: string) => `kg-mcp-${stack}-${r}`;        // MCP server resources

const location = cfg.get("location") ?? "eastus";

// Stable UUID for the MCP.Access app role — must not change between deployments
const MCP_ROLE_ID = "a1b2c3d4-e5f6-7890-abcd-ef0123456789";

// Azure AI Services connection constants (subscription owning the Cognitive Services resource)
const aiSvcConnSubId      = "ac039b59-e02c-43c1-a663-7dcb336c2626";
const aiSvcConnName       = "azure-ai-services";
const aiSvcConnResourceId = "/subscriptions/ac039b59-e02c-43c1-a663-7dcb336c2626/resourceGroups/rg-mychatbotdemio/providers/Microsoft.CognitiveServices/accounts/mychatbotdemio-resource";

// mcpApiUri is defined after tenantId (requires Pulumi Output — see Entra ID section)

// ── Sensitive config ───────────────────────────────────────────────────────
const neo4jPassword     = cfg.requireSecret("neo4jPassword");
const esKey             = cfg.requireSecret("esKey");
const azureAiApiKey     = cfg.requireSecret("azureAiApiKey");
const snowflakeOAuthClientId     = cfg.requireSecret("snowflakeOAuthClientId");
const snowflakeOAuthClientSecret = cfg.requireSecret("snowflakeOAuthClientSecret");

// ── Non-secret config ──────────────────────────────────────────────────────
const azureAiEndpoint          = cfg.require("azureAiEndpoint");
const azureAiModel             = cfg.get("azureAiModel") ?? "claude-haiku-4-5";
const azureAiFoundryGatewayUrl = cfg.get("azureAiFoundryGatewayUrl") ?? "";

// ══════════════════════════════════════════════════════════════════════════════
// AZURE RESOURCE GROUP + SHARED INFRA
// ══════════════════════════════════════════════════════════════════════════════

const rg = new azure_native.resources.ResourceGroup(n("rg"), {
    resourceGroupName: n("rg"),
    location,
    tags: { Stack: stack },
});

// ── Azure Container Registry (Basic — ~$5/mo, free-tier friendly) ──────────
const acrName = `kgchatbot${stack}`.replace(/[^a-z0-9]/gi, "").toLowerCase().substring(0, 50);

const registry = new azure_native.containerregistry.Registry(n("acr"), {
    resourceGroupName: rg.name,
    registryName:      acrName,
    location,
    sku:              { name: "Basic" },
    adminUserEnabled: true,
    tags:             { Stack: stack },
});

const acrCreds  = azure_native.containerregistry.listRegistryCredentialsOutput({
    resourceGroupName: rg.name,
    registryName:      registry.name,
});
const acrUser     = acrCreds.apply(c => c.username!);
const acrPassword = acrCreds.apply(c => c.passwords![0].value!);

// ── Log Analytics — free tier: 5 GB/month, daily cap enforced ─────────────
const logWorkspace = new azure_native.operationalinsights.Workspace(n("logs"), {
    resourceGroupName: rg.name,
    workspaceName:     n("logs"),
    location,
    sku:              { name: "PerGB2018" },
    retentionInDays:  30,
    workspaceCapping: { dailyQuotaGb: 0.16 },  // ~5 GB / 31 days
    tags:             { Stack: stack },
});

const logKeys = azure_native.operationalinsights.getSharedKeysOutput({
    resourceGroupName: rg.name,
    workspaceName:     logWorkspace.name,
});

// ── MCP Storage (Azure Blob — free tier: 5 GB for 12 months) ──────────────
const storageName = `kgmcp${stack}`.replace(/[^a-z0-9]/gi, "").toLowerCase().substring(0, 24);

const storageAccount = new azure_native.storage.StorageAccount(mn("sa"), {
    resourceGroupName: rg.name,
    accountName:       storageName,
    location,
    sku:               { name: "Standard_LRS" },
    kind:              "StorageV2",
    allowBlobPublicAccess: false,
    tags:              { Service: "mcp", Env: stack },
});

const blobContainer = new azure_native.storage.BlobContainer(mn("blob-container"), {
    resourceGroupName: rg.name,
    accountName:       storageAccount.name,
    containerName:     "data",
    publicAccess:      "None",
});

const csvFiles = [
    "lob", "cost_center", "application", "users",
    "user_app_access", "dremio_usage", "snowflake_usage", "s3_usage",
];
for (const table of csvFiles) {
    new azure_native.storage.Blob(mn(`blob-${table}`), {
        resourceGroupName: rg.name,
        accountName:       storageAccount.name,
        containerName:     blobContainer.name,
        blobName:          `${table}.csv`,
        source: new pulumi.asset.FileAsset(
            path.join(__dirname, "..", "mcp_server", "data", `${table}.csv`)
        ),
        contentType: "text/csv",
    });
}

const storageKeys = azure_native.storage.listStorageAccountKeysOutput({
    resourceGroupName: rg.name,
    accountName:       storageAccount.name,
});

const storageConnStr = pulumi.interpolate`DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageKeys.keys[0].value};EndpointSuffix=core.windows.net`;

// ══════════════════════════════════════════════════════════════════════════════
// COSMOS DB FOR MONGODB (chat history)
// NOTE: Cosmos DB provisioning failed in East US due to serverless capacity limits.
// Removed from Pulumi management 2026-04-09. The chatbot falls back to in-memory
// session history gracefully when MONGO_URI is not set.
// To restore: uncomment the block below, set mongoUri to the derived connection string,
// and re-run pulumi up from a region with serverless capacity (e.g. West US 2).
// ══════════════════════════════════════════════════════════════════════════════

// MONGO_URI not wired — Cosmos DB not provisioned; session.py falls back gracefully

// ══════════════════════════════════════════════════════════════════════════════
// CONTAINER APPS ENVIRONMENT
// Chat history persists in Azure Cosmos DB for MongoDB.
// ══════════════════════════════════════════════════════════════════════════════

const managedEnv = new azure_native.app.ManagedEnvironment(n("env"), {
    resourceGroupName: rg.name,
    environmentName:   n("env"),
    location,
    appLogsConfiguration: {
        destination: "log-analytics",
        logAnalyticsConfiguration: {
            customerId: logWorkspace.customerId,
            sharedKey:  logKeys.apply(k => k.primarySharedKey!),
        },
    },
    tags: { Stack: stack },
});

// Internal URL for the KB MCP server (chatbot → MCP, container-to-container)
const mcpInternalUrl = pulumi.interpolate`http://${mn("app")}`;

// Chatbot public FQDN — deterministic, available before container app is created
const chatbotFqdn    = pulumi.interpolate`chatbot-${stack}-app.${managedEnv.defaultDomain}`;
const chatbotBaseUrl = pulumi.interpolate`https://${chatbotFqdn}`;

// Azure AI Agents endpoint — derived from known constants, no dependency on aiProject resource
const agentsEndpointUri = pulumi.interpolate`https://${location}.api.azureml.ms/agents/v1.0/subscriptions/${aiSvcConnSubId}/resourceGroups/${rg.name}/providers/Microsoft.MachineLearningServices/workspaces/${n("ai-project")}`;

// ══════════════════════════════════════════════════════════════════════════════
// ENTRA ID (AZURE AD) — App Registrations + Users
// All Entra ID resources are free (Free tier: 50,000 MAU).
// ══════════════════════════════════════════════════════════════════════════════

// Current Azure client config — provides tenantId
const clientConfig = azure_native.authorization.getClientConfigOutput();
const tenantId     = clientConfig.tenantId;

// Identifier URI for the KB MCP server app registration.
// Must include tenant ID to satisfy Entra ID's app policy
// (policy requires: tenant verified domain, tenant ID, or app ID).
const mcpApiUri = pulumi.interpolate`api://${tenantId}/kg-mcp-server-${stack}`;

// Default Entra ID domain (e.g. contoso.onmicrosoft.com).
// getDomains requires Directory.Read.All; fall back to config or the known value
// if the Pulumi SP lacks that permission.
const defaultDomain = pulumi.output(
    azuread.getDomains({ onlyDefault: true })
        .then(d => d.domains[0].domainName)
        .catch(() => cfg.get("defaultDomain") ?? "bus28live.onmicrosoft.com")
);

// ── App Registration: KB MCP Server (the API / resource being protected) ────
// Exposes an app role "MCP.Access" that the chatbot is granted for 2LO.
const mcpServerApp = new azuread.Application("kg-mcp-server-app", {
    displayName:    "KG MCP Server",
    signInAudience: "AzureADMyOrg",
    identifierUris: [mcpApiUri],
    appRoles: [{
        allowedMemberTypes: ["Application"],
        description:   "Allows the chatbot to call the KB MCP server as an application (2LO)",
        displayName:   "MCP.Access",
        enabled:       true,
        id:            MCP_ROLE_ID,
        value:         "MCP.Access",
    }],
});

const mcpServerSp = new azuread.ServicePrincipal("kg-mcp-server-sp", {
    clientId:    mcpServerApp.clientId,
    useExisting: false,
});

// ── App Registration: KG Chatbot (the client — handles both user SSO and M2M) ─
// Supports authorization_code + PKCE for user login AND
// client_credentials (2LO) to call the KB MCP server.
const chatbotApp = new azuread.Application("kg-chatbot-app", {
    displayName:    "KG Chatbot",
    signInAudience: "AzureADMyOrg",
    web: {
        redirectUris:  [pulumi.interpolate`${chatbotBaseUrl}/auth/callback`],
        implicitGrant: {
            accessTokenIssuanceEnabled: false,
            idTokenIssuanceEnabled:     false,
        },
    },
    requiredResourceAccesses: [
        // Microsoft Graph — User.Read (delegated) for user info at login
        {
            resourceAppId: "00000003-0000-0000-c000-000000000000",
            resourceAccesses: [
                { id: "e1fe6dd8-ba31-4d61-89e7-88639da4683d", type: "Scope" },  // User.Read
            ],
        },
        // KB MCP Server — MCP.Access app role (application, 2LO)
        {
            resourceAppId: mcpServerApp.clientId,
            resourceAccesses: [{ id: MCP_ROLE_ID, type: "Role" }],
        },
    ],
});

const chatbotSp = new azuread.ServicePrincipal("kg-chatbot-sp", {
    clientId:    chatbotApp.clientId,
    useExisting: false,
});

// Grant admin consent: chatbot SP gets MCP.Access role on the MCP server SP.
// This enables client_credentials flow without interactive admin consent prompts.
const chatbotMcpRoleAssignment = new azuread.AppRoleAssignment("chatbot-mcp-access", {
    appRoleId:         MCP_ROLE_ID,
    principalObjectId: chatbotSp.objectId,
    resourceObjectId:  mcpServerSp.objectId,
});

// Chatbot client secret (auto-generated by Entra ID, stored in Container App secrets)
const chatbotSecret = new azuread.ApplicationPassword("kg-chatbot-secret", {
    applicationId: chatbotApp.id,
    displayName:   "chatbot-app-secret",
    endDate:       "2028-01-01T00:00:00Z",
});

// ── Entra ID Users ────────────────────────────────────────────────────────────
// NOTE: The Pulumi service principal needs "User Administrator" role in Entra ID
// to create users. Grant it in Azure Portal → Entra ID → Roles → User Administrator.

const user1 = new azuread.User("user-alast9", {
    userPrincipalName:          pulumi.interpolate`alast9@${defaultDomain}`,
    displayName:                "Alast 9",
    mailNickname:               "alast9",
    password:                   "habteq-niqNo2-matsyd",
    forcePasswordChange: false,
});

const user2 = new azuread.User("user-howard", {
    userPrincipalName:          pulumi.interpolate`howard.wang.us1@${defaultDomain}`,
    displayName:                "Howard Wang",
    mailNickname:               "howard.wang.us1",
    password:                   "rakdu6-cojqyR-jawtoz",
    forcePasswordChange: false,
});

// ══════════════════════════════════════════════════════════════════════════════
// MCP SERVER CONTAINER APP — internal, protected by 2LO JWT auth
// ══════════════════════════════════════════════════════════════════════════════

const mcpImage = new docker.Image(mn("image"), {
    build: {
        context:    path.join(__dirname, "..", "mcp_server"),
        dockerfile: path.join(__dirname, "..", "mcp_server", "Dockerfile"),
        platform:   "linux/amd64",
    },
    imageName: pulumi.interpolate`${registry.loginServer}/${mn("app")}:latest`,
    registry: {
        server:   registry.loginServer,
        username: acrUser,
        password: acrPassword,
    },
});

const mcpApp = new azure_native.app.ContainerApp(mn("app"), {
    resourceGroupName:    rg.name,
    containerAppName:     mn("app"),
    location,
    managedEnvironmentId: managedEnv.id,
    configuration: {
        ingress: {
            external:      false,   // internal only — not reachable from internet
            targetPort:    8001,
            transport:     "Http",
            allowInsecure: true,
        },
        registries: [{
            server:            registry.loginServer,
            username:          acrUser,
            passwordSecretRef: "acr-password",
        }],
        secrets: [
            { name: "acr-password",     value: acrPassword },
            { name: "storage-conn-str", value: storageConnStr },
        ],
    },
    template: {
        containers: [{
            name:  "mcp",
            image: mcpImage.repoDigest,
            resources: { cpu: 0.25, memory: "0.5Gi" },
            env: [
                { name: "PORT",                            value: "8001" },
                { name: "DUCKDB_AZURE_CONTAINER",          value: "data" },
                { name: "AZURE_STORAGE_ACCOUNT_NAME",      value: storageAccount.name },
                { name: "AZURE_STORAGE_CONNECTION_STRING", secretRef: "storage-conn-str" },
                // ── Entra ID JWT validation (2LO) ──────────────────────────
                { name: "ENTRA_TENANT_ID",   value: tenantId },
                { name: "ENTRA_MCP_API_URI", value: mcpApiUri },
            ],
        }],
        scale: { minReplicas: 1, maxReplicas: 1 },
    },
    tags: { Service: "mcp", Env: stack },
});

// ══════════════════════════════════════════════════════════════════════════════
// CHATBOT CONTAINER APP — public HTTPS, Entra ID SSO
// ══════════════════════════════════════════════════════════════════════════════

const chatbotImage = new docker.Image(n("image"), {
    build: {
        context:    path.join(__dirname, ".."),
        dockerfile: path.join(__dirname, "..", "Dockerfile"),
        platform:   "linux/amd64",
    },
    imageName: pulumi.interpolate`${registry.loginServer}/${n("app")}:latest`,
    registry: {
        server:   registry.loginServer,
        username: acrUser,
        password: acrPassword,
    },
});

const chatbotApp2 = new azure_native.app.ContainerApp(n("app"), {
    resourceGroupName:    rg.name,
    containerAppName:     `chatbot-${stack}-app`,
    location,
    managedEnvironmentId: managedEnv.id,
    configuration: {
        ingress: {
            external:      true,    // public HTTPS, Azure-managed TLS
            targetPort:    8000,
            transport:     "Http",
            allowInsecure: false,
        },
        registries: [{
            server:            registry.loginServer,
            username:          acrUser,
            passwordSecretRef: "acr-password",
        }],
        secrets: [
            { name: "acr-password",         value: acrPassword },
            { name: "entra-client-secret",  value: chatbotSecret.value },
            // mongo-uri removed — Cosmos DB not provisioned; session.py falls back to in-memory
            { name: "neo4j-password",       value: neo4jPassword },
            { name: "es-key",               value: esKey },
            { name: "azure-ai-api-key",     value: azureAiApiKey },
            { name: "dremio-pat",           value: cfg.requireSecret("DREMIO_PAT") },
        ],
    },
    template: {
        containers: [{
            name:  "app",
            image: chatbotImage.repoDigest,
            resources: { cpu: 0.5, memory: "1Gi" },
            env: [
                // ── App base URL (Entra ID redirect URI) ───────────────────
                { name: "APP_BASE_URL",                value: chatbotBaseUrl },

                // ── Claude on Azure AI (NOT anthropic.com) ─────────────────
                { name: "AZURE_AI_ENDPOINT",           value: azureAiEndpoint },
                { name: "AZURE_AI_MODEL",              value: azureAiModel },
                { name: "AZURE_AI_API_KEY",            secretRef: "azure-ai-api-key" },

                // ── Azure AI Foundry MCP Gateway (3LO Snowflake/Dremio) ────
                { name: "AZURE_AI_FOUNDRY_GATEWAY_URL", value: agentsEndpointUri },

                // ── Dremio MCP server ──────────────────────────────────────
                { name: "DREMIO_MCP_URL",              value: "https://mcp.dremio.cloud/mcp/dea2a74c-2f8a-4eef-8d40-c87db48d79ff" },

                // ── Internal KB MCP cost-analytics server ──────────────────
                { name: "MCP_BASE",                    value: mcpInternalUrl },

                // ── Entra ID — User SSO (authorization_code + PKCE) ───────
                { name: "ENTRA_TENANT_ID",             value: tenantId },
                { name: "ENTRA_CLIENT_ID",             value: chatbotApp.clientId },
                { name: "ENTRA_CLIENT_SECRET",         secretRef: "entra-client-secret" },

                // ── Entra ID — KB MCP 2LO (client_credentials) ────────────
                // Same ENTRA_CLIENT_ID/SECRET used; scope derived from ENTRA_MCP_API_URI.
                { name: "ENTRA_MCP_API_URI",           value: mcpApiUri },

                // ── Entra ID — AI Foundry gateway M2M scope (3LO) ─────────
                { name: "ENTRA_AI_SCOPE",
                  value: "https://cognitiveservices.azure.com/.default" },

                // ── Snowflake External OAuth SSO ───────────────────────────
                // The chatbot silently exchanges the user's Entra refresh_token
                // for a token scoped to this app, which Snowflake trusts via
                // the chatbot_entra_external_oauth security integration.
                { name: "SNOWFLAKE_ENTRA_APP_ID", value: "5daaa11c-aff1-48ac-b265-d6fc645bc669" },
                { name: "SNOWFLAKE_ACCOUNT",      value: "XJSKMFC-WQC92044" },

                // ── Dremio Cloud (REST API + OAuth) ────────────────────────
                // PAT is the service-account fallback; per-user OAuth via /auth/dremio/connect
                { name: "DREMIO_PROJECT_ID",           value: "dea2a74c-2f8a-4eef-8d40-c87db48d79ff" },
                { name: "DREMIO_PAT",                  secretRef: "dremio-pat" },
                // Native OAuth app — PKCE, no client_secret required
                { name: "DREMIO_OAUTH_CLIENT_ID",      value: "dc9160c2-76aa-4fab-9bc0-cd1bebe6da13" },

                // ── Data backends ──────────────────────────────────────────
                // MONGO_URI not set — Cosmos DB not provisioned; session.py falls back gracefully
                { name: "NEO4J_URI",                   value: "neo4j+s://afc6eb9c.databases.neo4j.io" },
                { name: "NEO4J_USER",                  value: "neo4j" },
                { name: "NEO4J_PASSWORD",              secretRef: "neo4j-password" },
                { name: "ES_URL",                      value: "https://my-elasticsearch-project-b00ed2.es.eastus.azure.elastic.cloud:443" },
                { name: "ES_IDX",                      value: "kg_descriptions" },
                { name: "ES_KEY",                      secretRef: "es-key" },
            ],
        }],
        scale: { minReplicas: 1, maxReplicas: 3 },
    },
    tags: { Service: "chatbot", Env: stack },
});

// Disable Container Apps Easy Auth so the Python app handles Entra ID OIDC itself.
// Without this, any previously-configured Easy Auth provider (e.g. Auth0) intercepts
// all requests at the platform level before they reach the app.
new azure_native.app.ContainerAppsAuthConfig(n("auth-config"), {
    resourceGroupName: rg.name,
    containerAppName:  chatbotApp2.name,
    authConfigName:    "current",
    platform: { enabled: false },
});

// ══════════════════════════════════════════════════════════════════════════════
// AZURE AI FOUNDRY HUB + PROJECT  (Agent Service — 3LO Snowflake/Dremio)
//
// Flow (3LO):
//   chatbot → Azure AI Foundry Project (Entra ID inbound auth)
//     → Snowflake MCP server (Snowflake OAuth authorization code grant)
//     → Dremio MCP server   (Dremio OAuth / API token)
//
// Inbound auth (Entra ID OIDC) is configured post-deploy via the
// Azure AI Projects SDK.  See docs/agent-inbound-auth.md.
//   Discovery URL: https://login.microsoftonline.com/{tenantId}/v2.0/.well-known/openid-configuration
//   Allowed client: chatbotApp.clientId
// ══════════════════════════════════════════════════════════════════════════════

// Key Vault (required by AI Foundry Hub) — free tier: 10,000 ops/month
const kvName   = `kg-kv-${stack}`.substring(0, 24);
const keyVault = new azure_native.keyvault.Vault(n("kv"), {
    resourceGroupName: rg.name,
    vaultName:         kvName,
    location,
    properties: {
        sku:                     { family: "A", name: "standard" },
        tenantId:                tenantId,
        enableRbacAuthorization: true,
        publicNetworkAccess:     "Enabled",
    },
    tags: { Stack: stack },
});

const aiHub = new mlv2025.Workspace(n("ai-hub"), {
    resourceGroupName: rg.name,
    workspaceName:     n("ai-hub"),
    location,
    kind:              "Hub",
    sku:               { name: "Basic", tier: "Basic" },
    identity:          { type: "SystemAssigned" },
    storageAccount:    storageAccount.id,
    keyVault:          keyVault.id,
    containerRegistry: registry.id,
    publicNetworkAccess: "Enabled",
    tags: { Stack: stack },
}, { customTimeouts: { create: "2h" } });

const aiProject = new mlv2025.Workspace(n("ai-project"), {
    resourceGroupName: rg.name,
    workspaceName:     n("ai-project"),
    location,
    kind:              "Project",
    sku:               { name: "Basic", tier: "Basic" },
    identity:          { type: "SystemAssigned" },
    hubResourceId:     aiHub.id,
    publicNetworkAccess: "Enabled",
    tags: { Stack: stack },
});


// WorkspaceConnection: Azure AI Services (for agent model calls)
// Pulumi's azure-native v2 SDK strips `resourceId` from AADAuthTypeWorkspaceConnectionProperties
// (it's not in the typed schema), causing a 400 ValidationError from the Azure API.
// Workaround: call the REST API directly via `az rest` so the full body is sent.

// az rest strips metadata.ResourceId (az ml CLI uses different wire format than raw REST).
// Use `az ml connection create` which correctly populates metadata.ResourceId.
// AIServices connections live on the Hub, not the Project.
// The create script is idempotent: skips creation if the connection already exists.
const aiServicesConn = new command.local.Command(n("ai-svc-conn"), {
    create: pulumi.all([rg.name, aiHub.name]).apply(([rgName, hubName]) => {
        const tmpFile = `/tmp/ai-svc-conn-${stack}.yaml`;
        const yaml = [
            `name: ${aiSvcConnName}`,
            `type: azure_ai_services`,
            `endpoint: "${azureAiFoundryGatewayUrl}"`,
            `api_key: placeholder`,
            `ai_services_resource_id: "${aiSvcConnResourceId}"`,
            `is_shared: true`,
        ].join("\\n");
        return [
            `printf '${yaml}' > ${tmpFile}`,
            `az ml connection show --name ${aiSvcConnName} --workspace-name ${hubName} --resource-group ${rgName} --subscription ${aiSvcConnSubId} > /dev/null 2>&1`,
            `|| az ml connection create --file ${tmpFile} --workspace-name ${hubName} --resource-group ${rgName} --subscription ${aiSvcConnSubId}`,
        ].join(" ");
    }),
    delete: pulumi.all([rg.name, aiHub.name]).apply(([rgName, hubName]) =>
        `az ml connection delete --name ${aiSvcConnName} --workspace-name ${hubName} --resource-group ${rgName} --subscription ${aiSvcConnSubId} --yes 2>/dev/null || true`
    ),
}, { dependsOn: [aiHub] });

// ARM ID of the connection — lives on the Hub, referenced by CapabilityHost on the Project.
const aiServicesConnId = pulumi.all([rg.name, aiHub.name]).apply(
    ([rgName, hubName]) =>
        `/subscriptions/${aiSvcConnSubId}/resourceGroups/${rgName}/providers/Microsoft.MachineLearningServices/workspaces/${hubName}/connections/${aiSvcConnName}`
);

// WorkspaceConnection: Azure Blob (agent thread storage)
const storageConn = new mlv2025.WorkspaceConnection(n("agent-storage-conn"), {
    resourceGroupName: rg.name,
    workspaceName:     aiProject.name,
    connectionName:    "agent-thread-storage",
    properties: {
        authType:    "AccountKey",
        category:    "AzureBlob",
        target:      pulumi.interpolate`https://${storageAccount.name}.blob.core.windows.net`,
        credentials: { key: storageKeys.keys[0].value },
        isSharedToAll: true,
        // Required metadata for AzureBlob connections
        metadata: {
            ContainerName: "data",
            AccountName:   storageAccount.name,
        },
    },
});

// NOTE: Snowflake MCP WorkspaceConnection and CapabilityHost removed 2026-04-09.
// Snowflake is now accessed directly via Entra ID External OAuth + SQL REST API
// (SNOWFLAKE_BACKEND=api in capabilities/snowflake.py). No Foundry Agent needed.
// To restore when Snowflake MCP bug is fixed: add back snowflakeMcpConn and capHost,
// set SNOWFLAKE_BACKEND=mcp, and re-run pulumi up.

// ── Exports ────────────────────────────────────────────────────────────────
export const url                    = chatbotBaseUrl;
export const acrLoginServer         = registry.loginServer;
export const storageAccountName     = storageAccount.name;
// cosmosAccountName removed — Cosmos DB not managed by Pulumi (provisioning failed)
export const resourceGroupName      = rg.name;
export const aiFoundryProjectName   = aiProject.name;
export const aiFoundryAgentsEndpoint = agentsEndpointUri;
export const aiFoundryHubName       = aiHub.name;

// Entra ID outputs (useful for post-deploy configuration)
export const entraTenantId          = tenantId;
export const entraChatbotClientId   = chatbotApp.clientId;
export const entraMcpServerClientId = mcpServerApp.clientId;
export const entraMcpApiUri         = mcpApiUri;
export const entraUser1Upn          = user1.userPrincipalName;
export const entraUser2Upn          = user2.userPrincipalName;
export const entraDefaultDomain     = defaultDomain;
