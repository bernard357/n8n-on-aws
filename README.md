# n8n-on-aws

**One YAML file, every shade of n8n on AWS.** A configuration-driven CLI that turns a typed,
validated YAML file into AWS CDK (Python) stacks. It covers everything from a single Fargate task to
multi-main HA on ECS or EKS, creates what is missing, and reuses what you already have.

```bash
n8n-on-aws init                                                   # assistant writes n8n.yaml
n8n-on-aws deploy -f fixtures/configuration/minimal.yaml --dry-run
n8n-on-aws deploy -f fixtures/configuration/minimal.yaml
```

> Status: design phase. Nothing is implemented yet; the research below drives the first release.

## Research and design

1. [Landscape and positioning](docs/research/01-landscape-and-positioning.md)
2. [Target users](docs/research/02-target-users.md)
3. [n8n deployment options mapped to AWS](docs/research/03-deployment-options.md)
4. [Configuration grammar](docs/research/04-configuration-grammar.md)
5. [CLI and assistant mode](docs/research/05-cli-and-assistant.md)
6. [n8n editions and licence enforcement](docs/research/06-licensing.md)
7. [FinOps posture](docs/research/07-finops.md)

## Sample configurations

From smallest to largest, in [fixtures/configuration](fixtures/configuration):

| File | Shade |
|---|---|
| `minimal.yaml` | Name + community usage declaration. Small profile: one Fargate task behind CloudFront, Aurora Serverless, new VPC |
| `nano.yaml` | Fargate Spot + SQLite on EFS behind CloudFront (no ALB, no NAT), Cognito at the edge, budget, office hours |
| `small-team.yaml` | Registered community, Aurora Serverless, CloudFront + Cognito with MFA, backups, alarms, budget |
| `queue-workers.yaml` | Business, queue mode, Spot workers scaled on queue depth, ElastiCache Serverless, S3 binary data, Entra ID via ALB OIDC |
| `split-webhooks.yaml` | Main + webhook + workers, CloudFront + WAF in front of the ALB, Cognito at the edge |
| `enterprise-ha.yaml` | Multi-main Enterprise, provisioned Aurora, KMS, S3 execution data, n8n OIDC SSO |
| `existing-landing-zone.yaml` | Brownfield ECS: existing VPC, cluster, Aurora, bucket, Cognito and certificate |
| `eks-new-cluster.yaml` | New EKS Auto Mode cluster with the official n8n Helm chart |
| `eks-existing-cluster.yaml` | Existing EKS cluster, external PostgreSQL and Redis |

The n8n version `2.37.1` in the samples is a placeholder for "a pinned release".
