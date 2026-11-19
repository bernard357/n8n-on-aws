# Changes

## Unreleased

- `n8n-on-aws init`, an assistant that writes a minimal, commented `n8n.yaml` (#9):
  - questions in the order of the decision tree: name and region, size with each profile's
    estimated cost, edition and declared use (Sustainable Use License), environment and office
    hours, ECS or EKS, network, database, queue, S3 files, ingress, domain and certificate, login
    wall and WAF, budget
  - read-only lookups through the AWS CLI (zones, certificates, VPCs and subnets, databases,
    caches, secrets, EKS clusters); `--offline` asks for every value instead
  - the file goes through the same rules as `validate`; nothing is written on errors; keys equal
    to the profile's are left out
  - `--from SAMPLE`, `--plain`, `-o`, `--force`, `--yes`; no new dependency
- Recycled from the `n8n-deployment` prototype (#15):
  - CI on GitHub Actions: lint, sample validation, tests and a JSON Schema check, without AWS
    credentials
  - a "Choosing a runtime" table in the README
  - `n8n.telemetry` (default `false`) sets `N8N_DIAGNOSTICS_ENABLED`, which n8n turns on by default
  - its EKS adapter and ECS-on-EC2 capacity code are kept on #11 and #10
- Monthly cost estimate (#8):
  - `plan` prints the estimate; `plan --cost` details fixed and usage parts per component, with
    the assumptions and the date of the prices; `--format json --cost` carries it for CI
  - prices from the AWS Price List API in a dated table for 13 regions, refreshed by
    `make prices`; `plan` makes no AWS call
  - `finops.usage` holds the traffic assumptions, with defaults per profile
  - `finops.max_monthly_usd` stops `plan` and `deploy` when the estimate is above it (`F2`)
- Teardown fixes from the smoke test of #4–#6:
  - the edge login wall's SSM parameter belongs to the us-east-1 stack; the n8n stack only fills
    it in, so no custom resource runs and recreates its log group during `destroy`
  - `n8n-on-aws cleanup` works for every deployment: it deletes the `/aws/lambda/<name>-*` log
    groups that CDK's helper functions leave, then the retained Lambda@Edge function and its
    log groups in every region
- AWS WAF and the IP allow-list on CloudFront (#7):
  - `ingress.cloudfront.waf: managed` creates a web ACL of scope CLOUDFRONT in us-east-1 with
    a rate limit and two AWS managed rule groups; `existing` attaches a global web ACL
  - `auth.mode: ip-allowlist` on CloudFront blocks other addresses except on the public paths
  - every web ACL also blocks `/metrics` whatever its case, when n8n serves it
  - one builder for the ALB and CloudFront web ACLs; dashboard "Firewall" section; web ACLs are
    part of the coverage check
  - rules `T6` (global scope for CloudFront; no allow-list on an existing web ACL), `F8` (cost)
  - `split-webhooks` now synthesizes
- Cognito login wall at CloudFront with Lambda@Edge, `auth.mode: cloudfront-cognito` (#6):
  - a viewer-request function in us-east-1 runs the code flow with PKCE on a public app client
    and checks the ID token on every editor request; no client secret exists
  - public paths have CloudFront behaviors without the function
  - the pool and client IDs reach the function through an SSM parameter in us-east-1
  - dashboard section "Login wall": Lambda@Edge errors seen by CloudFront, and invocations,
    errors and duration in the regional edge cache regions of the deployment's continent, where
    Lambda@Edge reports (a Paris user is served from eu-central-1 and eu-west-2, not eu-west-3).
    No alarm: CloudWatch alarms cannot read another region.
  - `n8n-on-aws cleanup` deletes the retained function and its log groups in every region, once
    AWS has removed the replicas
  - rules: `T7` (a domain is needed; at most 10 extra public paths), `F7` (cost)
  - `nano` and `small-team` now synthesize
- Worker scaling on queue depth and n8n metrics in CloudWatch (#5):
  - a CloudWatch agent sidecar on main publishes n8n's queue gauges to the `n8n-on-aws`
    namespace; `observability.prometheus: cloudwatch` adds five process metrics
  - workers scale out on waiting jobs, also from zero, and in when the jobs in flight fit on one
    worker less for `topology.workers.scale_in_minutes`; a CPU policy only scales out
  - `/metrics` answers 404 at the ALB and at CloudFront, because n8n serves it without authentication
  - dashboard sections "Queue jobs" and "n8n process", and a queue backlog alarm
  - rules `T16` (CPU scaling cannot start from zero workers; CPU target is a percentage) and `F6`
    (cost of the published metrics)
- Design assumptions checked against the n8n 2.48.6 source (#4):
  - the login wall now also lets through n8n's instance-level MCP server and its OAuth
    endpoints (`/mcp-server/*`, `/mcp-oauth/*`, `/.well-known/oauth-*`), signed binary-data
    links (`/rest/binary-data/signed`) and the OAuth client metadata document
  - `N8N_AVAILABLE_BINARY_DATA_MODES` is no longer set: n8n marks it safe to remove. The binary
    data mode stays explicit, because n8n defaults to `filesystem` outside queue mode.
  - FinOps finding `F3`: Aurora with `min_acu: 0` never pauses while n8n runs, because n8n keeps a
    connection open and pings every 2 seconds
- CloudWatch dashboard per deployment, generated from the synthesized resources (#3):
  - sections for CloudFront or the ALB, each n8n role, runners, logs, the database,
    queue, EFS, bucket, NAT and origin-updater Lambda, only when they exist
  - external resources are shown as "not monitored here"
  - a coverage test fails when a monitored resource type has no widget
  - `observability.dashboard` (`enabled`, `period_minutes`, `range`); off on `nano`
  - the `DashboardUrl` output; `plan` and `deploy --dry-run` show the dashboard
- Alarms are always created and notify only when emails are set. New alarms: main
  down, unhealthy targets, database capacity, CPU or storage, queue throttling or
  memory, EFS I/O limit, NAT port errors or instance status, origin-updater errors.
- The node-based ElastiCache replication group is named `<name>-queue`.
- Smoke test of `minimal` and a queue-mode shape (ALB, workers, ElastiCache
  Serverless, NAT, Container Insights): every dashboard widget showed data, except
  S3 storage metrics, which S3 reports daily and only for non-empty buckets. Fix:
  ElastiCache Serverless is reached with n8n's plain Redis client
  (`QUEUE_BULL_REDIS_HOST`), because its cluster client fails there over TLS and
  workers exited with "Failed to refresh slots cache".
- FinOps rule `F5`: dashboard and alarm costs; production without a dashboard.
- Smoke test of the split topology behind an HTTPS ALB with a Cognito login wall,
  on a real sub-domain: the editor and `/rest/*` redirect to Cognito, while
  `/webhook/*` and `/form/*` reach the webhook processors without a login. Every
  dashboard widget showed data. The log error tables no longer match n8n migration
  names such as `RemoveFailedExecutionStatus`.
- CDK synthesis on ECS Fargate (#2):
  - created or existing VPC, with NAT none, instance, single or per-AZ
  - Aurora Serverless v2, provisioned Aurora, RDS, external PostgreSQL, or SQLite on EFS
  - ElastiCache Serverless, node-based ElastiCache or external Redis
  - created or existing bucket with safe lifecycle rules; retained secrets; KMS; AWS Backup
  - one service per n8n role with an `n8nio/runners` sidecar
  - ALB with webhook routing, stickiness for multi-main, and Cognito or OIDC login
    that keeps webhook paths open; regional WAF
  - CloudFront to the ALB (VPC origin) or straight to the task (no ALB, no NAT)
  - budgets, cost anomaly alerts, office-hours schedules, alarms
- Smoke-tested `minimal` on a real account, which surfaced these fixes:
  - the runners sidecar image is pulled from Docker Hub (`n8nio/runners`), because
    `docker.n8n.io` does not serve it
  - ECS services are deleted before the cluster's capacity provider association, which
    otherwise blocks rollbacks and deletes
  - n8n 2.30 and later get `N8N_WEBHOOK_URL` only, not the deprecated `WEBHOOK_URL`
  - Lambda log groups are created with the stacks and deleted with them
  - `deploy` lists the regions that need a CDK bootstrap
- `deploy --dry-run` synthesizes locally. `deploy`, `diff` and `destroy` run the CDK CLI.
- Existing VPCs now list their `availability_zones`. `auth.oidc` lists the provider
  endpoints the ALB needs.
- Configuration model for `n8n-on-aws/v1`, as strict pydantic models with kind-discriminated
  `create` / `existing` / `external` resources, and `nano` to `xlarge` profiles.
- Rule engine:
  - topology and AWS rules (`T1`–`T15`)
  - n8n licence enforcement (`L1`–`L10`): community usage boundaries, edition-gated
    features, and gates that also apply to raw `n8n.env`
  - FinOps rules (`F1`–`F5`)
- CLI: `validate`, `plan` (text or JSON, `--show-env`), `deploy --dry-run`, `schema`,
  with `--set key=value` overrides.
- JSON Schema in `schema/n8n-on-aws.v1.json` for editor completion.
- Nine sample configurations, from `minimal` to `enterprise-ha`, on ECS and EKS.
- Research and design notes in `docs/research`.
