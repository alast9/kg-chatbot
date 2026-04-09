import * as pulumi from "@pulumi/pulumi";
import * as aws from "@pulumi/aws";
import * as awsx from "@pulumi/awsx";
import * as docker from "@pulumi/docker";
import * as path from "path";

const stack = pulumi.getStack();
const cfg   = new pulumi.Config();

// Naming helpers
const n  = (r: string) => `kg-chatbot-${stack}-${r}`;   // chatbot resources
const mn = (r: string) => `kg-mcp-${stack}-${r}`;        // MCP server resources

// ── Sensitive config ───────────────────────────────────────────────────────
// Set secrets via: pulumi config set --secret <key> "<value>"
const mongoUri          = cfg.requireSecret("mongoUri");
const neo4jPassword     = cfg.requireSecret("neo4jPassword");
const esKey             = cfg.requireSecret("esKey");
const auth0ClientSecret     = cfg.requireSecret("auth0ClientSecret");      // M2M app secret (sso.py)
const auth0OidcClientSecret = cfg.requireSecret("auth0OidcClientSecret");  // Web app secret (oidc.py)
const bedrockToken      = cfg.requireSecret("bedrockToken");

// ══════════════════════════════════════════════════════════════════════════════
// SHARED INFRASTRUCTURE
// ══════════════════════════════════════════════════════════════════════════════

// ── VPC (shared) ───────────────────────────────────────────────────────────
const vpc = new awsx.ec2.Vpc(n("vpc"), {
    numberOfAvailabilityZones: 2,
    natGateways:               { strategy: "Single" },
    tags:                      { Stack: stack },
});

// ── ElastiCache Redis ──────────────────────────────────────────────────────
// In-VPC Redis- no external credentials, no SSL needed.
// Access controlled purely by the security group (appSg → port 6379).
const redisSg = new aws.ec2.SecurityGroup(n("redis-sg"), {
    vpcId:       vpc.vpcId,
    description: "ElastiCache Redis- port 6379 from chatbot ECS tasks only",
    egress: [{ protocol: "-1", fromPort: 0, toPort: 0, cidrBlocks: ["0.0.0.0/0"] }],
    tags:   { Service: "chatbot", Env: stack },
});

const redisSubnetGroup = new aws.elasticache.SubnetGroup(n("redis-subnets"), {
    subnetIds: vpc.privateSubnetIds,
    tags:      { Service: "chatbot", Env: stack },
});

const redisCluster = new aws.elasticache.ReplicationGroup(n("redis"), {
    description:              "Chat session sliding-window cache",
    nodeType:                 "cache.t4g.micro",
    numCacheClusters:         1,
    engine:                   "redis",
    engineVersion:            "7.1",
    port:                     6379,
    subnetGroupName:          redisSubnetGroup.name,
    securityGroupIds:         [redisSg.id],
    atRestEncryptionEnabled:  true,
    transitEncryptionEnabled: false,  // private VPC- network isolation via SG
    automaticFailoverEnabled: false,
    tags:                     { Service: "chatbot", Env: stack },
});

// ══════════════════════════════════════════════════════════════════════════════
// MCP SERVER (DuckDB cost-analytics backend)
// Internal ECS service- not reachable from the internet.
// ══════════════════════════════════════════════════════════════════════════════

// ── ECR ────────────────────────────────────────────────────────────────────
const mcpRepo = new aws.ecr.Repository(mn("ecr"), {
    name:               `kg-mcp-${stack}`,
    imageTagMutability: "MUTABLE",
    forceDelete:        true,
    tags:               { Service: "mcp", Env: stack },
});

new aws.ecr.LifecyclePolicy(mn("ecr-lifecycle"), {
    repository: mcpRepo.name,
    policy: JSON.stringify({
        rules: [{
            rulePriority: 1,
            description:  "Keep last 5 images",
            selection:    { tagStatus: "any", countType: "imageCountMoreThan", countNumber: 5 },
            action:       { type: "expire" },
        }],
    }),
});

const mcpAuthToken = aws.ecr.getAuthorizationTokenOutput({ registryId: mcpRepo.registryId });

const mcpImage = new docker.Image(mn("image"), {
    build: {
        context:    path.join(__dirname, "..", "mcp_server"),
        dockerfile: path.join(__dirname, "..", "mcp_server", "Dockerfile"),
        platform:   "linux/amd64",
    },
    imageName: pulumi.interpolate`${mcpRepo.repositoryUrl}:latest`,
    registry: {
        server:   mcpRepo.repositoryUrl,
        username: mcpAuthToken.apply(t => t.userName),
        password: mcpAuthToken.apply(t => t.password),
    },
});

// ── ECS Cluster ────────────────────────────────────────────────────────────
const mcpCluster = new aws.ecs.Cluster(mn("cluster"), {
    tags: { Service: "mcp", Env: stack },
});

// ── CloudWatch Logs ────────────────────────────────────────────────────────
const mcpLogGroup = new aws.cloudwatch.LogGroup(mn("logs"), {
    name:            `/ecs/kg-mcp-${stack}`,
    retentionInDays: 7,
});

// ── IAM: Task Execution Role ───────────────────────────────────────────────
const mcpExecRole = new aws.iam.Role(mn("exec-role"), {
    assumeRolePolicy: aws.iam.assumeRolePolicyForPrincipal({ Service: "ecs-tasks.amazonaws.com" }),
});

new aws.iam.RolePolicyAttachment(mn("exec-role-attach"), {
    role:      mcpExecRole.name,
    policyArn: "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
});

// ── S3 Data Bucket ─────────────────────────────────────────────────────────
const dataBucket = new aws.s3.Bucket(mn("data"), {
    bucket:       `kg-mcp-data-${stack}`,
    forceDestroy: true,
    tags:         { Service: "mcp", Env: stack },
});

// Block all public access
new aws.s3.BucketPublicAccessBlock(mn("data-block"), {
    bucket:                dataBucket.id,
    blockPublicAcls:       true,
    blockPublicPolicy:     true,
    ignorePublicAcls:      true,
    restrictPublicBuckets: true,
});

// Upload CSV data files
const csvFiles = [
    "lob", "cost_center", "application", "users",
    "user_app_access", "dremio_usage", "snowflake_usage", "s3_usage",
];
for (const table of csvFiles) {
    new aws.s3.BucketObject(mn(`data-${table}`), {
        bucket: dataBucket.id,
        key:    `${table}.csv`,
        source: new pulumi.asset.FileAsset(
            path.join(__dirname, "..", "mcp_server", "data", `${table}.csv`)
        ),
        contentType: "text/csv",
    });
}

// ── IAM: Task Role (scoped to the data bucket) ────────────────────────────
const mcpTaskRole = new aws.iam.Role(mn("task-role"), {
    assumeRolePolicy: aws.iam.assumeRolePolicyForPrincipal({ Service: "ecs-tasks.amazonaws.com" }),
});

new aws.iam.RolePolicy(mn("task-s3-policy"), {
    role: mcpTaskRole.id,
    policy: pulumi.all([dataBucket.arn]).apply(([bucketArn]) => JSON.stringify({
        Version: "2012-10-17",
        Statement: [{
            Effect:   "Allow",
            Action:   ["s3:GetObject"],
            Resource: [`${bucketArn}/*`],
        }, {
            Effect:   "Allow",
            Action:   ["s3:ListBucket"],
            Resource: [bucketArn],
        }],
    })),
});

// ── Security Groups ────────────────────────────────────────────────────────
// mcpAlbSg: internal ALB- accepts port 80 only from the chatbot task SG
// (appSg is declared later; we forward-reference via its ID after creation)
const mcpAlbSg = new aws.ec2.SecurityGroup(mn("alb-sg"), {
    vpcId:       vpc.vpcId,
    description: "MCP internal ALB- port 80 from chatbot tasks only",
    egress: [{ protocol: "-1", fromPort: 0, toPort: 0, cidrBlocks: ["0.0.0.0/0"] }],
    tags:   { Service: "mcp", Env: stack },
});

const mcpAppSg = new aws.ec2.SecurityGroup(mn("app-sg"), {
    vpcId:       vpc.vpcId,
    description: "MCP ECS tasks- port 8001 from MCP ALB only",
    ingress: [{
        protocol:       "tcp",
        fromPort:       8001,
        toPort:         8001,
        securityGroups: [mcpAlbSg.id],
    }],
    egress: [{ protocol: "-1", fromPort: 0, toPort: 0, cidrBlocks: ["0.0.0.0/0"] }],
    tags:   { Service: "mcp", Env: stack },
});

// ── Internal Application Load Balancer ─────────────────────────────────────
const mcpAlb = new aws.lb.LoadBalancer(mn("alb"), {
    internal:         true,   // not reachable from the internet
    loadBalancerType: "application",
    securityGroups:   [mcpAlbSg.id],
    subnets:          vpc.privateSubnetIds,
    idleTimeout:      60,
    tags:             { Service: "mcp", Env: stack },
});

const mcpTg = new aws.lb.TargetGroup(mn("tg"), {
    port:       8001,
    protocol:   "HTTP",
    targetType: "ip",
    vpcId:      vpc.vpcId,
    healthCheck: {
        enabled:            true,
        path:               "/health",
        interval:           30,
        timeout:            10,
        healthyThreshold:   2,
        unhealthyThreshold: 3,
    },
    deregistrationDelay: 30,
    tags: { Service: "mcp", Env: stack },
});

new aws.lb.Listener(mn("listener"), {
    loadBalancerArn: mcpAlb.arn,
    port:            80,
    protocol:        "HTTP",
    defaultActions:  [{ type: "forward", targetGroupArn: mcpTg.arn }],
});

// ── ECS Task Definition ────────────────────────────────────────────────────
const mcpTaskDef = new aws.ecs.TaskDefinition(mn("task"), {
    family:                  mn("app"),
    cpu:                     "256",
    memory:                  "512",
    networkMode:             "awsvpc",
    requiresCompatibilities: ["FARGATE"],
    executionRoleArn:        mcpExecRole.arn,
    taskRoleArn:             mcpTaskRole.arn,
    containerDefinitions: pulumi.all([mcpImage.repoDigest, mcpLogGroup.name, dataBucket.bucket]).apply(
        ([imgName, lgName, bucketName]) => JSON.stringify([{
            name:      "mcp",
            image:     imgName,
            essential: true,
            portMappings: [{ containerPort: 8001, protocol: "tcp" }],
            environment: [
                { name: "PORT",               value: "8001" },
                { name: "AWS_REGION",         value: "us-east-1" },
                { name: "DUCKDB_S3_BUCKET",   value: bucketName },
            ],
            logConfiguration: {
                logDriver: "awslogs",
                options: {
                    "awslogs-group":         lgName,
                    "awslogs-region":        "us-east-1",
                    "awslogs-stream-prefix": "ecs",
                },
            },
        }])
    ),
});

// ── ECS Service ────────────────────────────────────────────────────────────
new aws.ecs.Service(mn("svc"), {
    cluster:        mcpCluster.arn,
    taskDefinition: mcpTaskDef.arn,
    desiredCount:   1,
    launchType:     "FARGATE",
    networkConfiguration: {
        subnets:        vpc.privateSubnetIds,
        securityGroups: [mcpAppSg.id],
        assignPublicIp: false,
    },
    loadBalancers: [{
        targetGroupArn: mcpTg.arn,
        containerName:  "mcp",
        containerPort:  8001,
    }],
    tags: { Service: "mcp", Env: stack },
});

// ══════════════════════════════════════════════════════════════════════════════
// CHATBOT
// Public-facing ECS service behind CloudFront → ALB.
// ══════════════════════════════════════════════════════════════════════════════

// ── ECR ────────────────────────────────────────────────════════════════════
const repo = new aws.ecr.Repository(n("ecr"), {
    name:               `kg-chatbot-${stack}`,
    imageTagMutability: "MUTABLE",
    forceDelete:        true,
    tags:               { Service: "chatbot", Env: stack },
});

new aws.ecr.LifecyclePolicy(n("ecr-lifecycle"), {
    repository: repo.name,
    policy: JSON.stringify({
        rules: [{
            rulePriority: 1,
            description:  "Keep last 5 images",
            selection:    { tagStatus: "any", countType: "imageCountMoreThan", countNumber: 5 },
            action:       { type: "expire" },
        }],
    }),
});

const authToken = aws.ecr.getAuthorizationTokenOutput({ registryId: repo.registryId });

const image = new docker.Image(n("image"), {
    build: {
        context:    path.join(__dirname, ".."),
        dockerfile: path.join(__dirname, "..", "Dockerfile"),
        platform:   "linux/amd64",
    },
    imageName: pulumi.interpolate`${repo.repositoryUrl}:latest`,
    registry: {
        server:   repo.repositoryUrl,
        username: authToken.apply(t => t.userName),
        password: authToken.apply(t => t.password),
    },
});

// ── ECS Cluster ────────────────────────────────────────────────────────────
const cluster = new aws.ecs.Cluster(n("cluster"), {
    tags: { Service: "chatbot", Env: stack },
});

// ── CloudWatch Logs ────────────────────────────────────────────────────────
const logGroup = new aws.cloudwatch.LogGroup(n("logs"), {
    name:            `/ecs/kg-chatbot-${stack}`,
    retentionInDays: 7,
});

// ── Secrets Manager ────────────────────────────────────────────────────────
const secret = new aws.secretsmanager.Secret(n("secret"), {
    name:                 `/kg-chatbot/${stack}/app`,
    description:          "Sensitive credentials for kg-chatbot",
    recoveryWindowInDays: 0,
});

new aws.secretsmanager.SecretVersion(n("secret-v1"), {
    secretId: secret.id,
    secretString: pulumi.jsonStringify({
        MONGO_URI:                mongoUri,
        NEO4J_PASSWORD:           neo4jPassword,
        ES_KEY:                   esKey,
        AUTH0_CLIENT_SECRET:      auth0ClientSecret,
        AUTH0_OIDC_CLIENT_SECRET: auth0OidcClientSecret,
        AWS_BEARER_TOKEN_BEDROCK: bedrockToken,
    }),
});

// ── IAM: Task Execution Role ───────────────────────────────────────────────
const execRole = new aws.iam.Role(n("exec-role"), {
    assumeRolePolicy: aws.iam.assumeRolePolicyForPrincipal({ Service: "ecs-tasks.amazonaws.com" }),
});

new aws.iam.RolePolicyAttachment(n("exec-role-attach"), {
    role:      execRole.name,
    policyArn: "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
});

new aws.iam.RolePolicy(n("exec-secrets-policy"), {
    role: execRole.id,
    policy: secret.arn.apply(arn => JSON.stringify({
        Version: "2012-10-17",
        Statement: [{ Effect: "Allow", Action: ["secretsmanager:GetSecretValue"], Resource: [arn] }],
    })),
});

// ── IAM: Task Role ─────────────────────────────────────────────────────────
const taskRole = new aws.iam.Role(n("task-role"), {
    assumeRolePolicy: aws.iam.assumeRolePolicyForPrincipal({ Service: "ecs-tasks.amazonaws.com" }),
});

new aws.iam.RolePolicy(n("task-bedrock-policy"), {
    role: taskRole.id,
    policy: JSON.stringify({
        Version: "2012-10-17",
        Statement: [{
            Effect:   "Allow",
            Action:   ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            Resource: ["*"],
        }],
    }),
});

// ── Security Groups ────────────────────────────────────────────────────────
const albSg = new aws.ec2.SecurityGroup(n("alb-sg"), {
    vpcId:       vpc.vpcId,
    description: "Chatbot ALB- HTTP/HTTPS from internet",
    ingress: [
        { protocol: "tcp", fromPort: 80,  toPort: 80,  cidrBlocks: ["0.0.0.0/0"] },
        { protocol: "tcp", fromPort: 443, toPort: 443, cidrBlocks: ["0.0.0.0/0"] },
    ],
    egress: [{ protocol: "-1", fromPort: 0, toPort: 0, cidrBlocks: ["0.0.0.0/0"] }],
    tags: { Service: "chatbot", Env: stack },
});

const appSg = new aws.ec2.SecurityGroup(n("app-sg"), {
    vpcId:       vpc.vpcId,
    description: "Chatbot ECS tasks- port 8000 from ALB only",
    ingress: [{
        protocol:       "tcp",
        fromPort:       8000,
        toPort:         8000,
        securityGroups: [albSg.id],
    }],
    egress: [{ protocol: "-1", fromPort: 0, toPort: 0, cidrBlocks: ["0.0.0.0/0"] }],
    tags: { Service: "chatbot", Env: stack },
});

// Allow chatbot tasks to reach ElastiCache Redis (port 6379)
new aws.ec2.SecurityGroupRule(n("redis-from-chatbot"), {
    type:                  "ingress",
    securityGroupId:       redisSg.id,
    protocol:              "tcp",
    fromPort:              6379,
    toPort:                6379,
    sourceSecurityGroupId: appSg.id,
    description:           "Allow chatbot ECS tasks to connect to Redis",
});

// Allow chatbot tasks to reach the MCP internal ALB (port 80)
new aws.ec2.SecurityGroupRule(mn("alb-from-chatbot"), {
    type:                  "ingress",
    securityGroupId:       mcpAlbSg.id,
    protocol:              "tcp",
    fromPort:              80,
    toPort:                80,
    sourceSecurityGroupId: appSg.id,
    description:           "Allow chatbot ECS tasks to call MCP ALB",
});

// ── Application Load Balancer ──────────────────────────────────────────────
const alb = new aws.lb.LoadBalancer(n("alb"), {
    internal:         false,
    loadBalancerType: "application",
    securityGroups:   [albSg.id],
    subnets:          vpc.publicSubnetIds,
    idleTimeout:      300,   // keep WebSocket connections alive
    tags:             { Service: "chatbot", Env: stack },
});

const tg = new aws.lb.TargetGroup(n("tg"), {
    port:       8000,
    protocol:   "HTTP",
    targetType: "ip",
    vpcId:      vpc.vpcId,
    healthCheck: {
        enabled:            true,
        path:               "/health",
        interval:           30,
        timeout:            10,
        healthyThreshold:   2,
        unhealthyThreshold: 3,
    },
    deregistrationDelay: 30,
    tags: { Service: "chatbot", Env: stack },
});

new aws.lb.Listener(n("listener"), {
    loadBalancerArn: alb.arn,
    port:            80,
    protocol:        "HTTP",
    defaultActions:  [{ type: "forward", targetGroupArn: tg.arn }],
});

// ── CloudFront Distribution ────────────────────────────────────────────────
// Terminates HTTPS for users; forwards HTTP to the chatbot ALB.
// AllViewer origin request policy: forwards Host, all query strings (Auth0
// code/state params), and cookies (session cookie).
const distribution = new aws.cloudfront.Distribution(n("cdn"), {
    origins: [{
        originId:   alb.dnsName,
        domainName: alb.dnsName,
        customOriginConfig: {
            httpPort:             80,
            httpsPort:            443,
            originProtocolPolicy: "http-only",
            originSslProtocols:   ["TLSv1.2"],
        },
    }],
    enabled:     true,
    httpVersion: "http2and3",
    defaultCacheBehavior: {
        targetOriginId:        alb.dnsName,
        viewerProtocolPolicy:  "redirect-to-https",
        allowedMethods:        ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"],
        cachedMethods:         ["GET", "HEAD"],
        cachePolicyId:         "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",  // CachingDisabled
        originRequestPolicyId: "216adef6-5c7f-47e4-b989-5492eafa07d3",  // AllViewer
    },
    restrictions: {
        geoRestriction: { restrictionType: "none" },
    },
    viewerCertificate: {
        cloudfrontDefaultCertificate: true,
    },
    tags: { Service: "chatbot", Env: stack },
});

// ── ECS Task Definition ────────────────────────────────────────────────────
const taskDef = new aws.ecs.TaskDefinition(n("task"), {
    family:                  n("app"),
    cpu:                     "512",
    memory:                  "1024",
    networkMode:             "awsvpc",
    requiresCompatibilities: ["FARGATE"],
    executionRoleArn:        execRole.arn,
    taskRoleArn:             taskRole.arn,
    containerDefinitions: pulumi.all([
        image.repoDigest,
        logGroup.name,
        secret.arn,
        distribution.domainName,
        mcpAlb.dnsName,
        redisCluster.primaryEndpointAddress,
    ]).apply(([imgName, lgName, secretArn, cfDomain, mcpDns, redisHost]) => JSON.stringify([{
        name:      "app",
        image:     imgName,
        essential: true,
        portMappings: [{ containerPort: 8000, protocol: "tcp" }],
        environment: [
            { name: "APP_BASE_URL",           value: `https://${cfDomain}` },
            { name: "AWS_REGION",             value: "us-east-1" },
            { name: "BEDROCK_MODEL",          value: "us.anthropic.claude-haiku-4-5-20251001-v1:0" },
            { name: "MONGO_DB",               value: "chatbot" },
            { name: "MONGO_COL",              value: "conversations" },
            { name: "REDIS_HOST",             value: redisHost },
            { name: "REDIS_PORT",             value: "6379" },
            { name: "REDIS_SSL",              value: "false" },
            { name: "NEO4J_URI",              value: "neo4j+s://afc6eb9c.databases.neo4j.io" },
            { name: "NEO4J_USER",             value: "neo4j" },
            { name: "ES_URL",                 value: "https://my-elasticsearch-project-b00ed2.es.eastus.azure.elastic.cloud:443" },
            { name: "ES_IDX",                 value: "kg_descriptions" },
            { name: "AUTH0_DOMAIN",           value: "dev-17z0ihexvjnnml4s.us.auth0.com" },
            { name: "AUTH0_CLIENT_ID",        value: "qbcFQbZwLGSjAQJYgt1BlvE6gvoGwvED" },
            { name: "AUTH0_AUDIENCE",         value: "urn:bedrock:agentcore:gateway" },
            // ── Auth0 OIDC (Web App — authorization_code for user login) ──
            { name: "AUTH0_OIDC_CLIENT_ID",   value: "1W3pslG5I5Qiz7B5L3aEBF3ZIhdjki5a" },
            { name: "AGENTCORE_GATEWAY_URL",  value: "https://dremiodatagateway-imuszwvktk.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp" },
            { name: "DREMIO_MCP_URL",         value: "https://mcp.dremio.cloud/mcp/dea2a74c-2f8a-4eef-8d40-c87db48d79ff" },
            { name: "MCP_BASE",               value: `http://${mcpDns}` },
        ],
        secrets: [
            { name: "MONGO_URI",                valueFrom: `${secretArn}:MONGO_URI::` },
            { name: "NEO4J_PASSWORD",           valueFrom: `${secretArn}:NEO4J_PASSWORD::` },
            { name: "ES_KEY",                   valueFrom: `${secretArn}:ES_KEY::` },
            { name: "AUTH0_CLIENT_SECRET",      valueFrom: `${secretArn}:AUTH0_CLIENT_SECRET::` },
            { name: "AUTH0_OIDC_CLIENT_SECRET", valueFrom: `${secretArn}:AUTH0_OIDC_CLIENT_SECRET::` },
            { name: "AWS_BEARER_TOKEN_BEDROCK", valueFrom: `${secretArn}:AWS_BEARER_TOKEN_BEDROCK::` },
        ],
        logConfiguration: {
            logDriver: "awslogs",
            options: {
                "awslogs-group":         lgName,
                "awslogs-region":        "us-east-1",
                "awslogs-stream-prefix": "ecs",
            },
        },
    }])),
});

// ── ECS Service ────────────────────────────────────────────────────────────
new aws.ecs.Service(n("svc"), {
    cluster:        cluster.arn,
    taskDefinition: taskDef.arn,
    desiredCount:   1,
    launchType:     "FARGATE",
    networkConfiguration: {
        subnets:        vpc.privateSubnetIds,
        securityGroups: [appSg.id],
        assignPublicIp: false,
    },
    loadBalancers: [{
        targetGroupArn: tg.arn,
        containerName:  "app",
        containerPort:  8000,
    }],
    tags: { Service: "chatbot", Env: stack },
});

// ── Exports ────────────────────────────────────────────────────────────────
export const redisEndpoint   = redisCluster.primaryEndpointAddress;
export const url         = pulumi.interpolate`https://${distribution.domainName}`;
export const albUrl      = pulumi.interpolate`http://${alb.dnsName}`;
export const mcpAlbUrl   = pulumi.interpolate`http://${mcpAlb.dnsName}`;
export const ecrRepo     = repo.repositoryUrl;
export const mcpEcrRepo  = mcpRepo.repositoryUrl;
export const clusterName = cluster.name;
export const mcpClusterName  = mcpCluster.name;
export const mcpDataBucket   = dataBucket.bucket;
