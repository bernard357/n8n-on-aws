# 3. n8n deployment options, mapped to AWS

Sources: n8n docs on queue mode, external storage, database, task runners, deployment and execution
variables, SSO, and the [2.0 breaking changes](https://docs.n8n.io/changelog/v20-breaking-changes.md),
read 2026-11-18. Items marked **⚠ verify** are where the docs contradict each other or say nothing.

## 3.1 n8n process roles

| Role | Command | Serves | Scaling |
|---|---|---|---|
| **main** | `n8n start` | Editor UI, REST and public API, `/webhook-test/*`, triggers and schedules (leader), pruning | 1, or ≥ 2 with multi-main (Enterprise) |
| **webhook** | `n8n webhook` | `/webhook/*`, `/webhook-waiting/*` (and forms, see §3.6) | Horizontal, on CPU or requests |
| **worker** | `n8n worker --concurrency=N` | Executions pulled from the BullMQ queue (Redis) | Horizontal, on CPU or **queue depth** |
| **runner** | `n8nio/runners` image (sidecar) | Code node JS and Python (external mode) | One sidecar per main or worker that executes code |

From 2.0, task runners are on by default and internal mode is deprecated (removal planned for 3.0). In
production, **every executing container gets an `n8nio/runners` sidecar pinned to the same version as
`n8nio/n8n`**, sharing `N8N_RUNNERS_AUTH_TOKEN`, with `N8N_RUNNERS_MODE=external`.

## 3.2 Topology ladder (folding vs. splitting)

| Topology | Processes | Requires | Use when |
|---|---|---|---|
| `single` | 1 main (regular mode) + runner | SQLite **on EFS** or PostgreSQL | Trial, personal, < ~10 executions/minute |
| `queue` | 1 main + N workers | PostgreSQL + Redis, `EXECUTIONS_MODE=queue` everywhere | Steady load, long or CPU-heavy workflows |
| `split` | 1 main + M webhook + N workers | as `queue` + ALB path routing, `N8N_DISABLE_PRODUCTION_MAIN_PROCESS=true` | Webhook bursts must not slow the editor |
| `multi-main` | ≥ 2 main + M webhook + N workers | as `split` + **Enterprise** + sticky sessions + `N8N_MULTI_MAIN_SETUP_ENABLED=true` | HA editor and triggers, zero-downtime upgrades |

"Folded" is `single`: everything in one task. "Split" separates the front-end (main, webhook) from the
execution engine (workers). `OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true` is recommended in queue topologies.
Without it, main also needs a runner sidecar for manual runs.

## 3.3 Axes and AWS options

### Runtime

| Option | AWS building blocks | Notes |
|---|---|---|
| `ecs` + `fargate` (**default**) | ECS service per role, Fargate arm64 (Graviton) | Serverless, cheapest to operate |
| `ecs` + `fargate-spot` | Capacity provider strategy (base on Fargate, rest on Spot) | Workers only, recommended. Main on Spot loses triggers on interruption |
| `ecs` + `ec2` | ASG capacity provider, Graviton | Steady large loads, cheaper per vCPU |
| `eks` (new cluster) | EKS **Auto Mode** (default) or managed node group, or Fargate profiles | Official n8n Helm chart, KEDA on Redis, Pod Identity, AWS LB Controller |
| `eks` (existing cluster) | Namespace + Helm release + Pod Identity associations | We don't own the cluster's add-ons; we verify they exist |

### Database (PostgreSQL; MySQL is removed in 2.0)

| Option | Notes |
|---|---|
| `sqlite` | `single` topology only, on **EFS** (Fargate tasks are ephemeral). Not valid with queue. |
| `aurora-serverless` (**default** for create) | Aurora PostgreSQL Serverless v2, `min_acu: 0` allows scale-to-zero (adds cold-start latency, see note). Optional readers. |
| `aurora` | Provisioned instances, Multi-AZ, optional I/O-Optimized |
| `rds` | RDS PostgreSQL, single-AZ or Multi-AZ, gp3 |
| `external` | "Alien" database: an existing Aurora or RDS in the account, or anywhere reachable. Host, port, database, schema, plus a Secrets Manager secret for credentials. Optional security group to authorize ingress. TLS via `DB_POSTGRESDB_SSL_*`; for AWS, mount the RDS CA bundle. |

Note: with `min_acu: 0`, n8n's frequent DB pings (`DB_PING_INTERVAL_SECONDS=2`) and the leader's schedule
checks probably keep Aurora awake. Scale-to-zero only pays off if the n8n tasks also scale to zero.
**⚠ verify** experimentally. Size pool usage too: `DB_POSTGRESDB_POOL_SIZE` (default 2) × tasks ×
roles < `max_connections`.

Aurora DSQL is **excluded**: it is not compatible enough with PostgreSQL for n8n's TypeORM migrations
(foreign keys, sequences).

### Queue (Redis protocol; required for `queue`, `split`, `multi-main`)

| Option | Notes |
|---|---|
| `elasticache-serverless` (**default** for create) | Valkey, TLS mandatory (`QUEUE_BULL_REDIS_TLS=true`). It exposes a cluster-mode endpoint, so BullMQ needs a hash-tagged prefix (`QUEUE_BULL_PREFIX={n8n}`) and probably `QUEUE_BULL_REDIS_CLUSTER_NODES`. **⚠ verify** with n8n before making it the default; fall back to `elasticache` otherwise. |
| `elasticache` | Valkey or Redis OSS node-based, cluster mode disabled, 1 primary + replicas, Multi-AZ failover |
| `external` | Existing Redis endpoint + auth secret, TLS flag |

Sentinel is not documented by n8n, so we don't support it. Use `N8N_REDIS_KEY_PREFIX` when Redis is shared.

### Binary and execution data

| Setting | Options | License |
|---|---|---|
| `binary_data.mode` | `database` (queue default in 2.x), `filesystem` (`single` only, needs EFS), `s3` | `s3`: Business/Enterprise |
| `execution_data.mode` | `database` (default), `s3` | `s3`: Business/Enterprise |
| bucket | `create` (SSE-KMS or S3, Block Public Access, TLS-only policy, lifecycle on `workflows/` prefix only) or `existing` (name + optional KMS key, prefix) | |

S3 auth uses the task or pod role with `N8N_EXTERNAL_STORAGE_S3_AUTH_AUTO_DETECT=true`, so there are no
access keys. Never put a whole-bucket lifecycle rule on a bucket that holds execution data. Versioned
buckets need `NoncurrentVersionExpiration`. **⚠ verify**: the docs disagree on 2.x defaults for
`N8N_DEFAULT_BINARY_DATA_MODE` and on whether `N8N_AVAILABLE_BINARY_DATA_MODES` still exists. We set
both explicitly based on `n8n.version`.

Beyond n8n's own use, the same bucket can be exposed to workflows ("heavy data exchanges") through the
role's IAM policy on an `exchange/` prefix, so the AWS S3 node needs no static keys.

### Ingress

Two first-class paths: **CloudFront** (`ingress.kind: cloudfront`, default for small teams, no fixed
floor when its origin is the task itself) and **ALB** (`ingress.kind: alb`, default for AWS practitioners
from `medium` up). Costs and origin choices are in [07-finops.md](07-finops.md).

| Option | Notes |
|---|---|
| ALB (**default**) | HTTPS with an ACM certificate (create with a Route 53 zone, or bring an existing ARN). Path rules for `split`. Sticky sessions for `multi-main`. Idle timeout ≥ 120 s for push (websocket/SSE). |
| ALB internal | Private n8n (VPN or Direct Connect users), no internet exposure |
| CloudFront in front of the ALB | Edge TLS, WAF, Cognito at the edge. The origin is locked with a CloudFront-prefix-list SG plus a secret header. `N8N_PROXY_HOPS=2` (CloudFront + ALB), otherwise 1. |
| No domain | ALB DNS name over HTTP: allowed only for `nano` or `dev`, with a warning |

n8n must know its public URL: `N8N_HOST`, `N8N_PROTOCOL=https`, `N8N_EDITOR_BASE_URL`, and
`WEBHOOK_URL` / `N8N_WEBHOOK_URL` (the latter from 2.30.0; we set both).

### Authentication wrapper (independent of n8n's own user management)

| Mode | How | Cost / complexity |
|---|---|---|
| `none` | n8n's built-in owner and users login only | — |
| `ip-allowlist` | ALB SG or WAF IP set | trivial |
| `alb-cognito` | ALB listener `authenticate-cognito` before forwarding editor paths | **No Lambda.** Recommended default when a login wall is wanted |
| `alb-oidc` | ALB `authenticate-oidc` with any IdP (Entra ID, Okta, Google) | No Lambda |
| `cloudfront-cognito` | CloudFront + Lambda@Edge (`cognito-at-edge` pattern), config baked at synth time, function stack in us-east-1 | Needed when CloudFront/WAF is used anyway, or for a global edge |
| `n8n-sso` | n8n's own OIDC or SAML (`N8N_SSO_*`, env-managed from 2.18.0) | Business/Enterprise. Can be combined with a wrapper |

Cognito can be `create` (user pool, domain, app client, optional MFA, optional federated IdP) or
`existing` (pool ID or ARN + client ID + domain).

**Paths that must bypass the wrapper**, otherwise external callers break: `/webhook/*`,
`/webhook-waiting/*`, `/form/*`, `/form-waiting/*`, `/mcp/*` (MCP Server Trigger), `/healthz*`, plus
user-declared extras. `/rest/oauth2-credential/callback` must stay behind the wrapper, because the user's
browser carries the cookie. `/webhook-test/*` and `/form-test/*` stay protected. **⚠ verify** the full
list against the n8n version, and allow `auth.public_paths` overrides.

### Network

| Option | Notes |
|---|---|
| `create` | 2–3 AZs, public and private subnets, `nat: none \| single \| per-az`, VPC endpoints (S3 gateway always; ECR, Logs, Secrets Manager, STS optional) |
| `existing` | VPC ID + private subnet IDs (+ public subnet IDs if internet-facing). Optional existing SGs. Lookup by tags also supported. |
| `nat: none` | Possible only if n8n needs no internet egress, which is rare for n8n. Allowed with a warning. |

### Secrets and keys

- `N8N_ENCRYPTION_KEY`: generated into Secrets Manager with **RemovalPolicy RETAIN**, or `existing` (to
  migrate an install). Injected into every role. Losing it makes every credential unreadable.
- License activation key: `existing` secret only (never inline in YAML).
- DB credentials: generated by RDS (managed secret) or `existing`. Runner auth token: generated.
- Optional customer-managed KMS key (`existing` ARN or `create`) for RDS, ElastiCache, S3, Logs, Secrets.

### Observability and operations

CloudWatch Logs retention, Container Insights, n8n `/metrics` (`N8N_METRICS=true`) scraped by CloudWatch
agent or AMP, alarms (5xx, CPU, queue depth, DB ACU or CPU) to an SNS email, AWS Backup plan for
DB and EFS, and execution pruning (`EXECUTIONS_DATA_MAX_AGE`, `EXECUTIONS_DATA_PRUNE_MAX_COUNT`).

## 3.4 License-gated features (self-hosted)

Superseded by the fuller table and enforcement rules in [06-licensing.md](06-licensing.md), which also
covers the registered community edition and the community usage boundaries.

| Feature | Community | Business | Enterprise |
|---|---|---|---|
| Queue mode, workers, webhook processors | ✓ | ✓ | ✓ |
| External binary or execution data (S3) | — | ✓ | ✓ |
| SSO (OIDC, SAML) | — | ✓ | ✓ |
| Multi-main | — | — | ✓ |
| Workers view in UI | — | — | ✓ |

## 3.5 Validation rules (the rule engine)

Hard errors:

1. `topology ∈ {queue, split, multi-main}` ⇒ `database.kind ≠ sqlite` and `queue.kind ≠ none`.
2. `topology = single` ⇒ `queue.kind = none` (warn and ignore otherwise), `main.replicas = 1`.
3. `topology = multi-main` ⇒ `license.edition = enterprise` and `main.replicas ≥ 2`. Enables ALB stickiness.
4. `binary_data.mode = s3` or `execution_data.mode = s3` or `auth.mode = n8n-sso` ⇒ `license.edition ∈ {business, enterprise}`.
5. `license.edition` paid ⇒ `license.activation_key` or `license.certificate` set (see 06-licensing.md).
6. `database.kind = sqlite` or `binary_data.mode = filesystem` ⇒ `topology = single` and EFS is provisioned.
7. `runtime.platform = eks` ⇒ `ingress.domain` set (the ALB hostname is not known at synth time).
8. `auth.mode ∈ {alb-cognito, alb-oidc, cloudfront-cognito}` ⇒ HTTPS (domain + certificate).
9. `auth.mode = cloudfront-cognito` ⇒ `ingress.kind = cloudfront`; `auth.mode ∈ {alb-cognito, alb-oidc}` ⇒ an ALB exists (`kind = alb` or CloudFront `origin: alb`).
10. Every `existing` reference must be fully specified (for example, an existing VPC needs private subnets).
11. `network.vpc` existing + `database` create ⇒ ≥ 2 private subnets in distinct AZs.
12. Fargate CPU and memory pairs must be valid.

Warnings:

- `n8n.version: latest` or `stable` (pin for production). The runners image is always derived from the same version.
- `ingress.domain` missing → plain HTTP.
- `fargate-spot` on main.
- `nat: none`.
- `database.deletion_protection: false` in an `environment: production`.
- `workers.concurrency < 5` (n8n advice).
- Estimated DB connections (pool × tasks) above 80 % of the instance class limit.

## 3.6 Open questions to settle with spikes

1. ElastiCache Serverless with BullMQ in n8n (hash tags, cluster client).
2. Exact 2.x binary-data defaults and env var survival.
3. Complete list of public paths (forms, MCP, chat trigger, waiting webhooks) per n8n version.
4. Aurora scale-to-zero with n8n's ping loop.
5. CloudFront + push backend: websocket through CloudFront works, but SSE may need cache and timeout tuning.
