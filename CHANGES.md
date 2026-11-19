# Changes

## Unreleased

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
