# Changes

## Unreleased

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
- `deploy --dry-run` synthesizes locally. `deploy`, `diff` and `destroy` run the CDK CLI.
- Existing VPCs now list their `availability_zones`. `auth.oidc` lists the provider
  endpoints the ALB needs.
- Configuration model for `n8n-on-aws/v1`, as strict pydantic models with kind-discriminated
  `create` / `existing` / `external` resources, and `nano` to `xlarge` profiles.
- Rule engine:
  - topology and AWS rules (`T1`–`T15`)
  - n8n licence enforcement (`L1`–`L10`): community usage boundaries, edition-gated
    features, and gates that also apply to raw `n8n.env`
  - FinOps rules (`F1`–`F4`)
- CLI: `validate`, `plan` (text or JSON, `--show-env`), `deploy --dry-run`, `schema`,
  with `--set key=value` overrides.
- JSON Schema in `schema/n8n-on-aws.v1.json` for editor completion.
- Nine sample configurations, from `minimal` to `enterprise-ha`, on ECS and EKS.
- Research and design notes in `docs/research`.
