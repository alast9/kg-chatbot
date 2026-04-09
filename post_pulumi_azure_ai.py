from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

client = AIProjectClient(
    endpoint="https://mychatbotdemio-resource.cognitiveservices.azure.com/",
    credential=DefaultAzureCredential()
)
agent = client.agents._create_agent(
    model="claude-haiku-4-5",
    name="kg-snowflake-agent",
    instructions="You are a data analyst with access to Snowflake data.",
    # Inbound auth — Auth0 OIDC
    # discovery_url: https://dev-17z0ihexvjnnml4s.us.auth0.com/.well-known/openid-configuration
    # allowed_clients: qbcFQbZwLGSjAQJYgt1BlvE6gvoGwvED, 1W3pslG5I5Qiz7B5L3aEBF3ZIhdjki5a,
    #                  LKHR2X7rZkMheUfsgJrvPmYMsJZtQxjU, AJFqhtLSzU3G3MSRiT5a3lC3jcXhrI0M
)

