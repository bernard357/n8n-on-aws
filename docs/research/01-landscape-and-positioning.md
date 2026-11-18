# 1. Landscape and positioning

Research date: 2026-11-18. Facts below come from the pages cited; GitHub star counts and
maturity are as observed on that date.

## 1.1 What exists today

### Vendor-maintained

| Project | Shape | Strengths | Limits for our audience |
|---|---|---|---|
| [n8n-io/terraform-aws-n8n](https://github.com/n8n-io/terraform-aws-n8n) ([docs](https://docs.n8n.io/deploy/host-n8n/install-options/use-a-cloud-provider/deploy-to-aws-with-terraform)) | Terraform, EKS + official Helm chart, RDS PostgreSQL, ElastiCache Redis, S3, ALB, KEDA, Pod Identity | Official, production-grade, multi-main HA, worker autoscaling on queue depth, sizing examples (small ≈ $440/mo, medium ≈ $2,000/mo, large ≈ $21,000+/mo) | Pre-release (`~> 0.5`), breaking changes expected. **EKS only.** Needs a pre-existing VPC. Default topology needs an **Enterprise** license (Business with one main). No ECS, no serverless floor, no auth wrapper. MIT. |
| [n8n official Helm chart](https://github.com/n8n-io/n8n-hosting) | Kubernetes manifests / Helm | Canonical for any Kubernetes | Not AWS-aware: no database, cache, bucket, IAM or DNS |
| [n8n on AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-mtvym4rmmm3fe) | CloudFormation templates (Community and Enterprise) | One click | Opaque, fixed shapes, hard to evolve |

### Community IaC

| Project | Tool | Shape | Notes |
|---|---|---|---|
| [elasticscale/terraform-aws-n8n](https://github.com/elasticscale/terraform-aws-n8n) | Terraform | One Fargate Spot task, EFS, ALB | ≈ $3/month claim. Single instance only; its README warns that `desired_count > 1` breaks webhooks. No PostgreSQL. Old image pin (1.4.0). GPL-3.0. |
| [vumdao/aws-cdk-self-hosted-n8n-infra](https://github.com/vumdao/aws-cdk-self-hosted-n8n-infra) ([post](https://dev.to/aws-builders/self-hosted-n8n-on-aws-ecs-with-aws-cdk-typescript-l6d)) | CDK TypeScript | Fargate main + worker, RDS Multi-AZ, ElastiCache, NLB, Cloud Map | Queue mode, but one fixed shape; configuration only through env vars |
| [azmimengu/aws-cdk-self-hosted-n8n-infra](https://github.com/azmimengu/aws-cdk-self-hosted-n8n-infra) | CDK | Queue mode, Aurora PostgreSQL Multi-AZ | Fixed shape |
| [netroy/cdk-fargate-n8n](https://github.com/netroy/cdk-fargate-n8n) | CDK | Fargate + RDS | Author marks it as unmaintained |
| [ajitalapati/n8n-aws-ecs](https://github.com/ajitalapati/n8n-aws-ecs) | CDK | Fargate + HTTPS | No queue mode |
| [pradip-allwyn/terraform-aws-n8n](https://github.com/pradip-allwyn/terraform-aws-n8n) | Terraform | ECS | Small community module |
| [simplynadaf/secure-n8n-on-aws](https://dev.to/aws-builders/secure-self-hosted-n8n-on-aws-the-2026-hardening-checklist-4jkk) | Guide + repo | Hardening checklist | Guidance more than a product |
| Blog walkthroughs ([Terraform + Okta OIDC](https://dev.to/anderson_leite/self-hosting-n8n-on-aws-ecs-fargate-with-terraform-okta-oidc-sso-and-a-shared-alb-rds-1p06), [CDK on Builder Center](https://builder.aws.com/content/2uAfOnMfsniW5oYKCpHEePpx81c/deploy-n8n-in-aws-using-ecs-services-and-cdk), [OpenTofu](https://oneuptime.com/blog/post/2026-03-20-deploy-n8n-workflow-automation-opentofu/view), [CNCF on EKS](https://www.cncf.io/blog/2026/10/16/deploying-maintaining-and-migrating-production-n8n-on-eks/)) | various | One author's architecture each | Copy-paste starting points, not maintained tools |
| Community Helm charts ([8gears](https://github.com/8gears/n8n-helm-chart), idirouhab) | Helm | Kubernetes, queue mode | Not AWS-aware |
| Marketplace AMIs (several vendors) | EC2 AMI | n8n + local PostgreSQL + systemd on one VM | Not IaC, not serverless, pets not cattle |

No result came from `aws-samples` or AWS Solutions, and no Pulumi project turned up.

### Building blocks we can reuse

- Cognito at the edge: [awslabs/cognito-at-edge](https://github.com/awslabs/cognito-at-edge) and
  [aws-samples/cloudfront-authorization-at-edge](https://github.com/aws-samples/cloudfront-authorization-at-edge).
  Lambda@Edge cannot read environment variables, so configuration has to be baked into the bundle at synth time.
- ALB's built-in `authenticate-cognito` / `authenticate-oidc` listener actions give the same login wall with
  **no Lambda at all**. This is a simpler option we should offer before edge Lambdas.

## 1.2 Gaps we observe

1. **Every project hard-codes one opinion.** Each one is a single shape, such as "1 Spot task + EFS" or
   "multi-main EKS for Enterprise". None of them covers the path from a $5 trial to multi-main HA with the
   same tool and the same file.
2. **ECS is under-served at the top end; EKS is under-served at the bottom end.** The official module is
   EKS-only and Enterprise-leaning. The ECS projects stop at a single worker service.
3. **Brownfield is an afterthought.** Real AWS accounts already have a landing-zone VPC, a shared RDS or
   Aurora, a Cognito pool, a central bucket or an EKS cluster. Most modules either create everything or
   need everything to exist already.
4. **n8n's own rules are not validated.** For example: multi-main needs Enterprise, S3 binary data and SSO
   need Business, queue mode needs PostgreSQL and Redis, the encryption key must be shared, and since 2.0
   task runners must be external with a version-matched `n8nio/runners` image. Users find these at runtime.
5. **No authentication wrapper.** n8n's own SSO is a paid feature. Community users who expose the editor to
   the internet have nothing in front of it, and webhook paths must stay open while the editor is protected.
6. **Serverless options are not used.** Aurora Serverless v2 can scale to 0 ACU, ElastiCache Serverless
   (Valkey) and Fargate Spot exist, yet most templates provision fixed instances.
7. **Nothing helps a newcomer write the configuration.** There is no assistant, no dry-run plan and no cost hint.

## 1.3 Positioning

> **n8n-on-aws: one YAML file, every shade of n8n on AWS.**
> A configuration-driven CLI that turns a typed, validated YAML file into CDK (Python) and CloudFormation
> stacks. It covers the smallest Fargate task to multi-main HA on ECS or EKS, creates what is missing and
> reuses what you already have.

What we are, and what we are not:

| We are | We are not |
|---|---|
| A **compiler** from an n8n deployment description to AWS. The YAML is the product. | Another fixed reference architecture |
| **n8n-aware**: we validate license tiers, mode and topology rules, version pinning, runner pairing and the shared encryption key before anything is synthesized | A generic container platform |
| **AWS-native**: CDK → CloudFormation, so stacks show up in the console, roll back cleanly, and drift detection works | A Terraform module, which competes head-on with the official one |
| **Brownfield-friendly**: every resource is either `create` or `existing` | A landing-zone tool |
| **Frugal by default**: ECS Fargate arm64, Aurora Serverless v2, one NAT or none | Optimized for Kubernetes purists (EKS is supported, not the default) |
| Complementary to n8n's Terraform: we use the **official Helm chart** on EKS rather than re-implementing it | A fork of n8n's chart |

**Differentiators, in order of weight**

1. One schema from `minimal` to `enterprise-ha`, with t-shirt `profile`s that set sane defaults and
   explicit overrides on top.
2. `create` or `existing` for VPC, database, cache, bucket, Cognito, certificate, hosted zone, EKS cluster
   and secrets.
3. A rule engine with n8n knowledge, run in `validate` and `deploy --dry-run`.
4. Authentication modes (`none`, `ip-allowlist`, `alb-cognito`, `alb-oidc`, `cloudfront-cognito`) that keep
   webhook paths open automatically.
5. `n8n-on-aws init`, an assistant that asks a few questions and writes a commented YAML file.
6. Pydantic models, which give a published JSON Schema and so editor completion in VS Code through
   `yaml-language-server`.

## 1.4 Blog post skeleton: "50 shades of n8n on AWS" (boringsteps.org)

1. **Why n8n on your own AWS?** Data residency, IAM-native access to Bedrock, S3 and RDS, cost at scale, and
   the community edition.
2. **The problem with templates.** Each existing project picks one shade (table from §1.1).
3. **Configuration-driven deployment.** Describe the deployment, don't script it. Show `minimal.yaml`
   (5 lines) next to `enterprise-ha.yaml` (60 lines), same tool.
4. **The axes that make the 50 shades.** Runtime × topology × database × queue × storage × auth × network
   × brownfield. Combining them gives far more than 50 valid shapes, and the rule engine rejects the
   invalid ones.
5. **Walk the ladder.** nano (SQLite on EFS, ~$5–15/mo) → small team → queue workers → split webhooks
   behind CloudFront + Cognito → multi-main Enterprise → existing landing zone on EKS.
6. **Dry-run first.** `n8n-on-aws deploy -f … --dry-run` prints the resolved plan, the stacks and
   indicative cost.
7. **Not sure where to start?** `n8n-on-aws init`.
8. What's next and how to contribute.

## Sources

- [n8n docs: Deploy to AWS with Terraform](https://docs.n8n.io/deploy/host-n8n/install-options/use-a-cloud-provider/deploy-to-aws-with-terraform)
- [n8n-io/terraform-aws-n8n](https://github.com/n8n-io/terraform-aws-n8n)
- [elasticscale/terraform-aws-n8n](https://github.com/elasticscale/terraform-aws-n8n)
- [vumdao/aws-cdk-self-hosted-n8n-infra](https://github.com/vumdao/aws-cdk-self-hosted-n8n-infra)
- [azmimengu/aws-cdk-self-hosted-n8n-infra](https://github.com/azmimengu/aws-cdk-self-hosted-n8n-infra)
- [netroy/cdk-fargate-n8n](https://github.com/netroy/cdk-fargate-n8n)
- [n8n on AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-mtvym4rmmm3fe)
- [CNCF: production n8n on EKS](https://www.cncf.io/blog/2026/10/16/deploying-maintaining-and-migrating-production-n8n-on-eks/)
- [awslabs/cognito-at-edge](https://github.com/awslabs/cognito-at-edge),
  [aws-samples/cloudfront-authorization-at-edge](https://github.com/aws-samples/cloudfront-authorization-at-edge)
- [Secure self-hosted n8n on AWS: 2026 hardening checklist](https://dev.to/aws-builders/secure-self-hosted-n8n-on-aws-the-2026-hardening-checklist-4jkk)
