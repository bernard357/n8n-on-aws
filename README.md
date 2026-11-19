# n8n-on-aws

**One YAML file, every shade of n8n on AWS.** A configuration-driven CLI that turns a typed,
validated YAML file into AWS CDK (Python) stacks. It covers everything from a single Fargate task to
multi-main HA on ECS or EKS, creates what is missing, and reuses what you already have.

```bash
n8n-on-aws init                                                   # assistant writes n8n.yaml
n8n-on-aws deploy -f fixtures/configuration/minimal.yaml --dry-run
n8n-on-aws deploy -f fixtures/configuration/minimal.yaml
```

> Status: the configuration model, the rule engine (topology, n8n licensing, FinOps) and
> CDK synthesis on **ECS Fargate** work. `minimal.yaml` was deployed to a real account
> (eu-west-3, n8n 2.48.5): n8n served its editor through CloudFront, connected to Aurora
> over verified TLS, and registered its JavaScript and Python task runners. Then it was
> destroyed. The other shapes are synthesized and asserted in tests, but not yet
> deployed. See the [roadmap](#roadmap).

## Quick start (from source)

```bash
git clone https://github.com/bernard357/n8n-on-aws.git && cd n8n-on-aws
make setup
.venv/bin/n8n-on-aws init                       # questions, then a commented n8n.yaml
.venv/bin/n8n-on-aws plan -f n8n.yaml --cost
.venv/bin/n8n-on-aws validate -f fixtures/configuration/nano.yaml
.venv/bin/n8n-on-aws plan -f fixtures/configuration/queue-workers.yaml --show-env
.venv/bin/n8n-on-aws plan -f fixtures/configuration/queue-workers.yaml --cost
.venv/bin/n8n-on-aws deploy -f fixtures/configuration/enterprise-ha.yaml --dry-run
.venv/bin/n8n-on-aws validate -f fixtures/configuration/minimal.yaml --set topology.mode=multi-main
```

The last command fails on purpose: multi-main needs the n8n Enterprise edition. Every
finding has a stable code: `S` schema, `T` topology, `L` licence, `F` FinOps, `C`
synthesis. Point your editor at [schema/n8n-on-aws.v1.json](schema/n8n-on-aws.v1.json)
for completion.

## Deploying

You need Node.js (for `npx aws-cdk@2`), AWS credentials, and a bootstrapped account and
region (`npx aws-cdk@2 bootstrap`, plus `us-east-1` when CloudFront needs a certificate).

```bash
.venv/bin/n8n-on-aws diff -f my-n8n.yaml
.venv/bin/n8n-on-aws deploy -f my-n8n.yaml
.venv/bin/n8n-on-aws destroy -f my-n8n.yaml
.venv/bin/n8n-on-aws cleanup -f my-n8n.yaml   # hours later, with cloudfront-cognito
```

CDK assets live in the bootstrap stack (`CDKToolkit`). It is not removed by `destroy`.

The deployment is split into stacks: `<name>-network`, `<name>-data`, `<name>-n8n`, and
`<name>-edge` in us-east-1 when needed. The encryption key secret, database snapshots,
the bucket and EFS are retained when stacks are deleted, except with `environment: dev`.
After the first deployment, open the URL printed as the `Url` output and create the n8n
owner account.

## Operating

Each deployment gets one CloudWatch dashboard, named after the deployment and printed as
the `DashboardUrl` output. It is generated from the resources that were synthesized, so it
follows the configuration:

- a header with the edition, version and links to n8n, its logs and its ECS cluster
- alarm status, and task counts per role when Container Insights is on
- CloudFront (metrics from us-east-1) or the load balancer
- CPU and memory of each n8n role, and of the runner sidecars with Container Insights
- Logs Insights tables of recent errors per role
- the database, queue, EFS, bucket, NAT and origin-updater Lambda, when they exist
- links to Cost Explorer, the budget and cost anomalies

Resources outside AWS (an external database or Redis, the NAT of an existing VPC) appear
as a note, never silently left out. A test checks that every resource of a monitored type
in the templates has a widget.

Alarms are always created, because the dashboard shows them. They send email only when
`observability.alarms.email` is set. Turn the dashboard off with
`observability.dashboard.enabled: false` (the default for `nano`).

## Research and design

1. [Landscape and positioning](docs/research/01-landscape-and-positioning.md)
2. [Target users](docs/research/02-target-users.md)
3. [n8n deployment options mapped to AWS](docs/research/03-deployment-options.md)
4. [Configuration grammar](docs/research/04-configuration-grammar.md)
5. [CLI and assistant mode](docs/research/05-cli-and-assistant.md)
6. [n8n editions and licence enforcement](docs/research/06-licensing.md)
7. [FinOps posture](docs/research/07-finops.md)

## Choosing a runtime

The n8n roles (main, webhook processors, workers) are the same everywhere; `runtime` only picks
the AWS building blocks that run them.

| You are… | Use | Status |
|---|---|---|
| New to n8n on AWS, small or medium workload, cost-sensitive | `runtime.platform: ecs` on Fargate, `ingress.kind: cloudfront` | ✅ |
| Handling many webhooks or a steady execution load | ECS on Fargate, `topology.mode: queue` or `split`, workers on Spot | ✅ |
| Holding Savings Plans or Reserved Instances, or needing a hardened AMI | ECS on EC2 (`runtime.ecs.capacity: ec2`) | planned [#10](https://github.com/bernard357/n8n-on-aws/issues/10) |
| Already running Kubernetes (Helm, GitOps, policies) | `runtime.platform: eks` | planned [#11](https://github.com/bernard357/n8n-on-aws/issues/11) |

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

## Roadmap

| Area | Status |
|---|---|
| Configuration model, profiles, licence and FinOps rules | ✅ [#1](https://github.com/bernard357/n8n-on-aws/issues/1) |
| ECS Fargate: network, data, services, ALB, CloudFront (ALB or task origin), Cognito and OIDC on the ALB, budgets, schedules | ✅ [#2](https://github.com/bernard357/n8n-on-aws/issues/2) |
| Smoke test of `minimal` (CloudFront to task, Aurora Serverless, runners) on a real account | ✅ |
| CloudWatch dashboard and alarms generated from the deployed resources | ✅ [#3](https://github.com/bernard357/n8n-on-aws/issues/3) |
| Smoke test of the ALB and queue shapes (Spot workers, ElastiCache Serverless, NAT) and of both dashboards | ✅ [#3](https://github.com/bernard357/n8n-on-aws/issues/3) |
| Smoke test of the split topology (webhook processors) behind an HTTPS ALB with a Cognito login wall | ✅ |
| Smoke test of multi-main (needs an n8n Enterprise licence) | not planned |
| Verify Aurora scale-to-zero, binary-data defaults and public paths (measured on a real account) | ✅ [#4](https://github.com/bernard357/n8n-on-aws/issues/4) |
| Worker scaling on queue depth (0 → 3 → 0 on a real account), n8n Prometheus metrics in CloudWatch | ✅ [#5](https://github.com/bernard357/n8n-on-aws/issues/5) |
| Cognito at the edge (`cloudfront-cognito`, Lambda@Edge): redirect and open paths smoke-tested | sign-in and `cleanup` to check [#6](https://github.com/bernard357/n8n-on-aws/issues/6) |
| WAF and IP allow-list on CloudFront | synthesized, smoke test pending [#7](https://github.com/bernard357/n8n-on-aws/issues/7) |
| Cost estimate in `plan`, `max_monthly_usd` guard | ✅ [#8](https://github.com/bernard357/n8n-on-aws/issues/8) |
| `n8n-on-aws init` assistant | ✅ [#9](https://github.com/bernard357/n8n-on-aws/issues/9) |
| ECS on EC2 capacity | planned [#10](https://github.com/bernard357/n8n-on-aws/issues/10) |
| EKS with the official Helm chart | planned [#11](https://github.com/bernard357/n8n-on-aws/issues/11) |
| Cross-region backup copies | planned [#12](https://github.com/bernard357/n8n-on-aws/issues/12) |
| Overview dashboard across deployments | planned [#13](https://github.com/bernard357/n8n-on-aws/issues/13) |
| CloudWatch Application Signals (spike) | planned [#14](https://github.com/bernard357/n8n-on-aws/issues/14) |

The EKS samples validate and plan, but synthesis stops with a `C1` finding until EKS lands.
