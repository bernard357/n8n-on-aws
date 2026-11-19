# 7. FinOps posture

Every option in the file has a cost. The tool's job is to make that cost **visible before deployment**,
pick **frugal defaults**, and make the expensive choices **explicit and justified**.

## 7.1 Principles

1. **Frugal by default, scale by declaration.** Defaults are Graviton (arm64), Fargate, serverless data
   services, a single NAT or none, short log retention, and S3 lifecycle rules. Anything more expensive is
   opt-in or comes from a larger `profile`.
2. **Cost is part of the plan.** `n8n-on-aws plan` and `deploy --dry-run` print a monthly estimate per
   component (fixed floor + usage-driven part), from the AWS Price List API, cached. They also print the
   delta when an option changes.
3. **Warn on the cost cliffs.** These are the choices that add a fixed monthly floor whatever the traffic
   (table §7.3).
4. **Tag everything for allocation.** `n8n-on-aws:deployment`, `n8n-on-aws:component` (ingress, compute,
   database, queue, storage, network, edge, observability), `n8n-on-aws:environment`, plus user tags. The
   `plan` output reminds users to activate them as cost allocation tags.
5. **Guardrails in the account.** Optional AWS Budget per deployment with email alerts at 80 % and 100 %,
   and optional Cost Anomaly Detection monitor on the deployment tag.
6. **Pay for idle only when asked.** Office-hours schedules for non-production environments scale ECS
   services to zero at night. Aurora Serverless can go to 0 ACU, but
   only while every n8n service is stopped, because n8n keeps a connection open. Spot is used where interruption is
   harmless (workers).

## 7.2 Two ingress paths, two audiences

| | `ingress.kind: cloudfront` (edge) | `ingress.kind: alb` |
|---|---|---|
| Audience | Small teams, cost-sensitive | AWS practitioners, private and enterprise networks |
| Fixed floor | ~$0 (CloudFront free tier: 1 TB and 10M requests a month) + Lambda@Edge per request | ALB ~$16–20/month + LCUs |
| Login wall | `cloudfront-cognito` (Lambda@Edge on editor requests only, ~$0.60 per million plus duration; webhooks bypass it; `F7`) | `alb-cognito`, `alb-oidc` (built in, no Lambda) |
| WAF | optional, at the edge | optional, on the ALB |
| Private (internal) n8n | no | yes (`public: false`) |
| Topologies | origin `task`: single main (with or without workers). Origin `alb`: any | any |

**CloudFront origin choices**

- `origin: task`: the main Fargate task gets a public IP in a public subnet. Its security group admits
  only the CloudFront origin-facing managed prefix list. An EventBridge rule plus a small Lambda keeps an
  `origin.<domain>` DNS record pointed at the task's current IP. **No ALB and no NAT gateway**, which is
  the cheapest path. Limits: one main replica, and a short outage on task replacement.
  CloudFront-to-origin TLS needs a certificate on the task, so this is a spike item (§7.5). Until then it
  is plain HTTP over the origin hop, with a warning.
- `origin: alb`: CloudFront in front of the ALB, used for WAF/edge or global users at any scale. The ALB
  is internal, behind a CloudFront VPC origin.

## 7.3 Cost cliffs (fixed monthly floors, eu-west-1, indicative)

| Choice | Floor | Frugal alternative |
|---|---|---|
| NAT gateway | ~$35/month each + $0.048/GB | `nat: none` with public-IP tasks (security group locked down), `nat: instance` (t4g.nano NAT instance, ~$4) |
| `nat: per-az` (3 AZs) | ~$105/month | `single` outside production |
| ALB | ~$18/month | CloudFront `origin: task` |
| Interface VPC endpoints | ~$8/month each per AZ | gateway endpoint for S3 (free), NAT for the rest |
| Public IPv4 | ~$3.6/month per task or EIP | IPv6/dual-stack later |
| Aurora provisioned `db.r7g.large` × 2 | ~$400/month | Aurora Serverless v2, or RDS `db.t4g.micro` (~$13) for small teams |
| Aurora Serverless v2, min 0.5 ACU | ~$45/month | `min_acu: 0` (scale to zero, cold start), RDS t4g |
| ElastiCache node `cache.r7g.large` | ~$170/month per node | ElastiCache Serverless Valkey (pay per GB-hour and ECPU), `cache.t4g.micro` |
| Container Insights (enhanced) | per metric | off by default |
| CloudWatch dashboard | first 3 per account free, then $3/month | `dashboard.enabled: false` (default on `nano`) |
| CloudWatch alarms | $0.10/month each (about 5 to 12 per deployment) | always on: they are the operator's first signal |
| CloudFront `price_class: all` | higher per-GB rates | `100` (NA + EU) |
| Log retention "never expire" | grows forever | 30 days default |
| S3 without lifecycle | grows forever | `binary_expiry_days`, `exchange.expiry_days` |

Figures are indicative and to be replaced by the Price List API output. They are listed here to justify
the defaults.

## 7.4 Grammar additions

```yaml
finops:
  budget:
    monthly_usd: float?            # creates an AWS Budget scoped to the deployment tag
    alert_emails: [email] = []
  anomaly_detection: bool = false
  schedule:                        # non-production only
    kind: always-on | office-hours = always-on
    timezone: str = UTC
    weekdays: "MON-FRI"
    start: "08:00"
    stop: "19:00"
  max_monthly_usd: float?          # plan fails if the estimate exceeds it (CI guard)

network:
  vpc:
    nat: none | instance | single | per-az

ingress:
  kind: cloudfront | alb           # default: cloudfront for nano/small, alb for medium+
  cloudfront:
    origin: task | alb
```

Rules:

- `finops.schedule.kind = office-hours` with `environment: production` → error.
- `ingress.cloudfront.origin = task` → `topology ∈ {single, queue}` and `main.replicas = 1`.
- `nat: none` → tasks get public IPs in public subnets. The paid-licence egress rule L9 is then satisfied.
- Estimate > `finops.max_monthly_usd` → `plan` exits non-zero.

## 7.4b The estimate (`plan --cost`)

`plan` prints a one-line monthly estimate, and `plan --cost` details it per component (compute,
network, ingress, edge, database, queue, storage, security, observability), in two columns:

- **fixed**: what the deployment costs at its minimum size with no traffic (tasks at `min`, Aurora at
  its floor, NAT, ALB, public IPv4, ElastiCache Serverless minimum, secrets, dashboard and alarms);
- **usage**: what follows the traffic assumptions of `finops.usage` (profile defaults when unset):
  requests, data out, data stored, logs, average ACU and workers above their minimum.

Prices come from `src/n8n_on_aws/finops/prices.json`, refreshed from the AWS Price List API by
`make prices` (on-demand, USD, dated). `plan` itself makes no AWS call. Regions without a table are
priced as us-east-1 with a note, and so are classes missing from the table. Global list prices
(WAF, Lambda@Edge, CloudFront, CloudWatch dashboards, alarms and metrics) are constants in
`estimate.py`. Modelling choices, each printed with the estimate:

- Fargate Spot is priced 70% below on-demand; Spot workers keep one on-demand task first.
- Aurora with `min_acu: 0` is priced at 0.5 ACU while n8n runs (it never pauses then, see #4), and
  for the office hours only when a schedule stops n8n.
- ElastiCache Serverless costs its 100 MB minimum plus about 7 million ECPUs a month of n8n's idle
  polling (measured); Redis OSS is priced as Valkey × 1.49 (serverless) or × 1.25 (nodes).
- 20% of data out goes through the NAT; 30% of requests reach the editor (Lambda@Edge).
- One million Aurora I/O a month per GB stored; LCUs from 25 new connections a second.
- Free tiers are shown as notes, except CloudFront's 1 TB and 10 million requests, which are
  always deducted. Taxes, support plans, credits and Savings Plans are not included.

`finops.max_monthly_usd` turns the estimate into a guard: `plan` exits 1 and `deploy` refuses to
run when the total is above it, with an `F2` error.

| Sample | Region | Fixed | Total |
|---|---|---:|---:|
| `nano` (office hours, SQLite, Spot) | us-east-1 | 3 | 4 |
| `minimal` | us-east-1 | 66 | 69 |
| `small-team` | eu-west-1 | 88 | 91 |
| `existing-landing-zone` | eu-west-1 | 89 | 95 |
| `queue-workers` | us-east-1 | 174 | 187 |
| `split-webhooks` | us-east-1 | 910 | 993 |
| `enterprise-ha` | eu-central-1 | 1679 | 2056 |

USD a month, prices of 2026-11-19, default assumptions of each profile.

## 7.5 Spikes

1. TLS on the `origin: task` hop: a Caddy sidecar with Let's Encrypt via the Route 53 DNS challenge, using
   the task role, against plain HTTP with the prefix-list lock.
2. ~~Price List API coverage and caching for the estimator~~: done in #8 with a dated table.
3. ECS scheduled scaling to zero and n8n's behaviour at start-up (missed schedules).
