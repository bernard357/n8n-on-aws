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
.venv/bin/n8n-on-aws validate -f fixtures/configuration/nano.yaml
.venv/bin/n8n-on-aws plan -f fixtures/configuration/queue-workers.yaml --show-env
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
```

CDK assets live in the bootstrap stack (`CDKToolkit`). It is not removed by `destroy`.

The deployment is split into stacks: `<name>-network`, `<name>-data`, `<name>-n8n`, and
`<name>-edge` in us-east-1 when needed. The encryption key secret, database snapshots,
the bucket and EFS are retained when stacks are deleted, except with `environment: dev`.
After the first deployment, open the URL printed as the `Url` output and create the n8n
owner account.

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

## Roadmap

| Area | Status |
|---|---|
| Configuration model, profiles, licence and FinOps rules | ✅ [#1](https://github.com/bernard357/n8n-on-aws/issues/1) |
| ECS Fargate: network, data, services, ALB, CloudFront (ALB or task origin), Cognito and OIDC on the ALB, budgets, schedules | ✅ [#2](https://github.com/bernard357/n8n-on-aws/issues/2) |
| Smoke test of `minimal` (CloudFront to task, Aurora Serverless, runners) on a real account | ✅ |
| Smoke tests of the ALB, Cognito, queue and multi-main shapes | next |
| Cognito at the edge (`cloudfront-cognito`), WAF and IP allow-list on CloudFront | planned |
| Worker scaling on queue depth | planned (CPU scaling meanwhile) |
| `n8n-on-aws init` assistant | planned |
| ECS on EC2 capacity, EKS (official Helm chart) | planned |
| Cost estimate in `plan`, cross-region backups, Prometheus | planned |

Samples that use a planned feature (`nano`, `small-team`, `split-webhooks`, and the EKS
ones) validate and plan, but synthesis stops with a `C1` finding that names the feature.
