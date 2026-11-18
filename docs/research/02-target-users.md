# 2. Target users

The tool is for people who **own an AWS account and want n8n inside it**, without designing the
architecture themselves. We describe six personas, from the most numerous to the most demanding. The
first three are the core; the last three are the reason the brownfield and EKS features exist.

| # | Persona | Typical shade | What they need from us | What they don't want |
|---|---|---|---|---|
| P1 | **Builder / solo automator.** An indie developer, consultant, or AI-agent tinkerer with an AWS account, often leaving n8n Cloud or a VPS. | `nano`, `minimal` | A 5-line YAML file, the lowest monthly bill, HTTPS on their domain, a login wall without a paid license, `destroy` that cleans up | Kubernetes, Redis, choosing subnet CIDRs |
| P2 | **Small/medium business ops team.** IT generalists automating CRM, ERP, and email flows for 5–50 users. | `small-team`, `queue-workers` | Managed PostgreSQL with backups, Cognito login for staff, predictable cost, safe upgrades, a guided `init` | Writing CDK or Terraform |
| P3 | **AWS-first platform or DevOps engineer** at a scale-up providing n8n as an internal service | `queue-workers`, `split-webhooks` | Queue mode with autoscaling workers, a webhook tier, S3 binary data, CloudWatch alarms, a GitOps-able file and CI-friendly `--dry-run` | Opaque Marketplace stacks, ClickOps |
| P4 | **Enterprise cloud team** with a landing zone, licensed n8n Enterprise or Business | `enterprise-ha`, `existing-landing-zone` | Use the **existing** VPC, Aurora, KMS keys, buckets, Cognito/IdP and permission boundaries. Multi-main HA, SSO via OIDC/SAML, tags, and CloudFormation that compliance can review | Resources created outside their guardrails, public subnets they did not approve |
| P5 | **Kubernetes shop** already running EKS | `eks-existing-cluster` | Deploy into an existing cluster and namespace with the official Helm chart, IRSA or Pod Identity, KEDA | A second cluster, or ECS |
| P6 | **AWS partners and consultants** deploying n8n for many customers | any, repeated | One file per customer, review in PRs, reproducible environments (dev/stage/prod), cost hints for proposals | Maintaining a fork per customer |

## Primary user and design consequences

- **Primary: P1–P3**, an AWS-literate user who is *not* an n8n infrastructure expert. This leads to:
  safe defaults, profiles, an assistant, n8n rule validation, and ECS before EKS.
- **Must not be blocked: P4–P5**. This leads to: `existing` on every resource, no hidden public exposure,
  permission boundary and tag propagation, CloudFormation outputs, and Helm on EKS.
- **Multiplier: P6**. This leads to: the file is the unit of reuse, `--dry-run` output is reviewable, and
  there is no state outside CloudFormation.

## Jobs to be done

1. *"Give me n8n on my domain, protected by a login, for the price of a coffee."* (P1)
2. *"Let my team use n8n safely, with backups and no single point of failure we can't recover from."* (P2)
3. *"Scale executions horizontally when webhooks spike, without re-architecting."* (P3)
4. *"Fit n8n into our landing zone and pass the security review."* (P4)
5. *"Run n8n like every other workload in our cluster."* (P5)
6. *"Stamp out the same n8n for the next customer, with a different size."* (P6)

## Non-goals (for now)

- Multi-region active/active. n8n multi-main is single-region; DR is via snapshot and re-deploy.
- Non-AWS clouds.
- Managing n8n workflows or credentials as code. That belongs to n8n's own source-control feature and CLI.
