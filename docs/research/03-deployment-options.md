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

Note on `min_acu: 0` (**checked against n8n 2.48.6 source on 2026-11-19**): an Aurora Serverless v2
instance pauses only when no user connection is open. n8n pings the database every 2 seconds
(`DB_PING_INTERVAL_SECONDS`) and closes idle pool connections only after 30 seconds
(`DB_POSTGRESDB_IDLE_CONNECTION_TIMEOUT`), so **Aurora never pauses while any n8n task runs**.
Scale-to-zero pays off only when an office-hours schedule stops every n8n service. The `F3` finding
says so. **Measured on 2026-11-19** (eu-west-3, n8n 2.48.6, `min_acu: 0`): capacity stayed at
0.5 ACU for 45 minutes of n8n uptime; once every n8n task was stopped, Aurora paused about 5 minutes
later. Starting main again, n8n reached its editor 20 seconds after its process started, against 5
seconds with a running database: resuming Aurora adds about 15 seconds.
Size pool usage too: `DB_POSTGRESDB_POOL_SIZE` (default 2) × tasks × roles < `max_connections`.

Aurora DSQL is **excluded**: it is not compatible enough with PostgreSQL for n8n's TypeORM migrations
(foreign keys, sequences).

### Queue (Redis protocol; required for `queue`, `split`, `multi-main`)

| Option | Notes |
|---|---|
| `elasticache-serverless` (**default** for create) | Valkey, TLS mandatory (`QUEUE_BULL_REDIS_TLS=true`). Its endpoint is a proxy that n8n reaches with a plain client (`QUEUE_BULL_REDIS_HOST`, not `QUEUE_BULL_REDIS_CLUSTER_NODES`: n8n's cluster client fails over TLS with "Failed to refresh slots cache", found by the #3 smoke test). Keys still live in slots, so BullMQ needs a hash-tagged prefix (`QUEUE_BULL_PREFIX={n8n}`). |
| `elasticache` | Valkey or Redis OSS node-based, cluster mode disabled, 1 primary + replicas, Multi-AZ failover |
| `external` | Existing Redis endpoint + auth secret, TLS flag |

Sentinel is not documented by n8n, so we don't support it. Use `N8N_REDIS_KEY_PREFIX` when Redis is shared.

### Worker scaling and n8n metrics

n8n publishes its queue as Prometheus gauges on the **main** instance only
(`N8N_METRICS_INCLUDE_QUEUE_METRICS=true`, refreshed every 20 seconds):
`n8n_scaling_mode_queue_jobs_waiting`, `…_active`, `…_completed` and `…_failed`. A job waits only
when every worker is at its concurrency, so one waiting job already means "more workers".

- A CloudWatch agent sidecar in the main task scrapes `localhost:5678/metrics` every 30 seconds and
  writes the selected metrics as EMF logs (`/n8n-on-aws/<name>/metrics`). CloudWatch turns them into
  metrics of the `n8n-on-aws` namespace, dimension `Deployment`.
- **Scale out**: step scaling on the waiting jobs (2 minutes): +1 worker from 1 job, +2 from `target`,
  +4 from 3 × `target`. It works from zero workers, so `workers.min: 0` is allowed with queue depth.
- **Scale in**: −1 worker when `waiting + active − (running − 1) × concurrency ≤ 0` for
  `scale_in_minutes`, that is, when the jobs in flight fit on one worker less. Running workers come
  from the `SampleCount` of the service's CPU metric, so Container Insights is not needed.
- **Safety net**: a CPU target (80 %) that only scales out. When the queue metrics are missing, the
  scaling alarms have no data and do nothing, so workers never scale in on a broken metric.
- `/metrics` has no authentication in n8n, so it answers **404 at the ALB** (listener rule) and **at
  CloudFront** (a CloudFront Function on `/metrics*`). Path matching is case-sensitive there, while
  n8n's router is not: behind `auth.mode: none`, `/METRICS` still reaches n8n. A login wall covers it.
- **Measured on 2026-11-19** (concurrency 5, `target: 5`, `scale_in_minutes: 3`, `min: 0`, `max: 3`):
  180 webhook calls, each job waiting 30 seconds. Jobs waited from 14:42; the scale-out alarm fired
  at 14:43:33 and set 3 workers, running by 14:45:39; 15 jobs ran at a time (3 × 5); all 181 jobs
  completed, none failed. After the queue emptied at 14:53, workers went 3 → 2 → 1 → 0 at 14:57,
  15:01 and 15:05.
- `observability.prometheus: cloudwatch` adds five process metrics (active workflows, event loop lag
  p99, heap, resident memory, leader). Each published metric costs about $0.30 a month (`F6`).

### Binary and execution data

| Setting | Options | License |
|---|---|---|
| `binary_data.mode` | `database` (queue default in 2.x), `filesystem` (`single` only, needs EFS), `s3` | `s3`: Business/Enterprise |
| `execution_data.mode` | `database` (default), `s3` | `s3`: Business/Enterprise |
| bucket | `create` (SSE-KMS or S3, Block Public Access, TLS-only policy, lifecycle on `workflows/` prefix only) or `existing` (name + optional KMS key, prefix) | |

S3 auth uses the task or pod role with `N8N_EXTERNAL_STORAGE_S3_AUTH_AUTO_DETECT=true`, so there are no
access keys. Never put a whole-bucket lifecycle rule on a bucket that holds execution data. Versioned
buckets need `NoncurrentVersionExpiration`. **Checked against n8n 2.48.6 source on 2026-11-19:**
when `N8N_DEFAULT_BINARY_DATA_MODE` is unset, n8n uses `filesystem` in regular mode and `database`
in queue mode. On Fargate, `filesystem` without EFS loses files with the task, so we always set the
mode. `N8N_AVAILABLE_BINARY_DATA_MODES` is deprecated ("safe to remove"), so we no longer set it. In
`database` mode, files are capped at 512 MiB (`N8N_BINARY_DATA_DATABASE_MAX_FILE_SIZE`, at most
1024 because of the PostgreSQL `bytea` limit).

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

**Paths that must bypass the wrapper**, otherwise external callers break. **Checked against the
routes n8n 2.48.6 registers without its own session (2026-11-19):**

| Path | Caller |
|---|---|
| `/webhook/*` | production webhooks, hosted chat of the Chat Trigger, Slack and Telegram human-in-the-loop replies |
| `/webhook-waiting/*`, `/form-waiting/*` | resume URLs of Wait nodes and waiting forms |
| `/form/*` | production forms (the pages are self-contained, no `/assets` needed) |
| `/mcp/*` | MCP Server Trigger |
| `/mcp-server/*`, `/mcp-oauth/*`, `/.well-known/oauth-*` | n8n's instance-level MCP server and the OAuth endpoints its clients use (n8n checks its own tokens) |
| `/rest/binary-data/signed` | signed links to binary data that workflows hand to callers |
| `/rest/oauth2-credential/client-metadata.json` | OAuth client metadata document, fetched by authorization servers |
| `/healthz*` | liveness and readiness |

`/rest/oauth2-credential/callback` must stay behind the wrapper, because the user's browser carries
the cookie. `/webhook-test/*`, `/form-test/*` and `/mcp-test/*` stay protected, as do the rest of
`/rest/*` and the editor. The public API (`/api/v1/*`, API key) is not opened by default; add it to
`auth.public_paths` when scripts call it from outside. The paths assume the default
`N8N_ENDPOINT_*` names.

### Cognito at the edge (`cloudfront-cognito`)

- A **viewer-request Lambda@Edge** function (Node.js 22, no dependencies) guards the default
  CloudFront behavior. Each public path gets its own behavior **without** the function, so webhook
  traffic passes and pays for no invocation.
- **No secret anywhere:** the app client is public, and the function runs the OAuth code flow with
  PKCE. Callback `/_auth/callback`, sign-out `/_auth/signout`. The ID token sits in an HttpOnly,
  Secure, SameSite=Lax cookie; the function checks its RS256 signature against the pool's JWKS, its
  issuer, audience, `token_use` and expiry on every request, and does not pass it on to n8n.
- **Configuration:** Lambda@Edge has no environment variables. The domain, paths and session length
  are written next to the code at synth time. The user pool and client are created in the
  deployment's region, after the us-east-1 stack, so the `<name>-n8n` stack writes their IDs to the
  SSM parameter `/n8n-on-aws/<name>/edge-auth` in us-east-1 (custom resource), and the function
  reads it once per cold start. Until then it answers 503 and retries.
- **Session:** the ID token lives `min(session_hours, 24)` hours (Cognito's maximum). When it
  expires, a page load goes back through Cognito (silent while the Cognito session lasts) and an
  API call gets 401, so the editor asks to reload. There is no refresh token.
- **Where it reports:** Lambda@Edge writes metrics and logs in the region of the regional edge cache
  that served the request, as `us-east-1.<name>-edge-auth`. Users near Paris were served from
  eu-central-1 and eu-west-2, not eu-west-3. The dashboard graphs the cache regions of the
  deployment's continent; there is no alarm, because CloudWatch alarms cannot read another region.
  CloudFront's own `LambdaExecutionError` metrics only exist once an error has happened.
- **Measured on 2026-11-19:** editor pages redirect to the Cognito hosted UI with PKCE, API calls get
  401, the public paths reach n8n, `/metrics` answers 404, and Cognito accepts the request.
  Signing in was not tested (no user was created).
- **Teardown:** AWS deletes Lambda@Edge replicas hours after the distribution stops using them,
  so the function is retained by `destroy`; `n8n-on-aws cleanup` deletes it later, with its log
  groups `/aws/lambda/us-east-1.<name>-edge-auth` in every region.

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
