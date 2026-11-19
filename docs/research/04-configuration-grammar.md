# 4. Configuration grammar (`n8n-on-aws/v1`)

## 4.1 Design principles

1. **One file, one deployment.** One file maps to one CDK app and to a set of CloudFormation stacks named
   `<name>-network`, `<name>-data`, `<name>-n8n`, plus `<name>-edge` in us-east-1 when needed.
2. **Describe n8n, not AWS.** Top-level sections follow n8n concepts (`topology`, `database`, `queue`,
   `storage`, `auth`). AWS-only knobs live under `runtime.ecs`, `runtime.eks` and `aws`.
3. **Every resource is `kind`-discriminated.** One key, `kind`, picks the implementation, and the other
   keys of that block belong to that kind only. Pydantic models this as a discriminated union, so a typo
   or a key that belongs to another kind is an error, not silently ignored.
4. **`create` or `existing` everywhere.** Each resource we might own (VPC, ECS or EKS cluster, database,
   cache, bucket, Cognito, certificate, hosted zone, KMS key, WAF, secrets) accepts `kind: existing` with
   identifiers, or an `external` kind when it lives outside AWS's control.
5. **Layered defaults.** Values resolve as: built-in defaults < `profile` < the file < `--set key=value`
   on the CLI. `n8n-on-aws plan` shows the resolved value and where each one came from.
6. **No secrets in YAML.** Secrets are always references: `{secret: <name-or-arn>}` or
   `{secret: <name>, key: <json-key>}`.
7. **Escape hatch.** `n8n.env` passes any n8n variable as-is, after the generated ones. A variable we
   generate can be overridden only with a warning.

## 4.2 Profiles (t-shirt sizes)

A profile only sets defaults; any explicit key wins.

| | `nano` | `small` (default) | `medium` | `large` | `xlarge` |
|---|---|---|---|---|---|
| topology | single | single | queue | split | multi-main |
| main (vCPU / GiB) | 0.25 / 0.5 | 0.5 / 1 | 1 / 2 | 1 / 2 | 2 / 4 × 2 |
| webhook | — | — | — | 1..4 × 0.5 / 1 | 2..10 × 1 / 2 |
| workers | — | — | 1..4 × 1 / 2 | 2..10 × 1 / 2 | 4..40 × 2 / 4 |
| database | sqlite (EFS) | aurora-serverless 0.5–2 ACU | aurora-serverless 0.5–8 | aurora-serverless 2–16, 1 reader | aurora r7g.large × 2 |
| queue | none | none | elasticache-serverless | elasticache-serverless | elasticache r7g, 1 replica |
| ingress | cloudfront (origin task) | cloudfront (origin task) | alb | alb | alb |
| nat | none | none | single | per-az | per-az |
| capacity | fargate-spot | fargate | fargate (workers spot) | fargate (workers spot) | fargate |
| indicative cost* | ~$15–25 | ~$60–90 | ~$150–250 | ~$400–700 | $1,500+ |

\*Ballpark figures for eu-west-1 at low traffic, dominated by NAT, ALB and database minimums. These are
to be calibrated with the AWS Pricing API in `plan --cost`.

## 4.3 Reference grammar

Notation: `type = default`, `a|b` is an enum, `?` is optional, `!` is required, `→` is a rule.

```yaml
apiVersion: n8n-on-aws/v1                # !
name: str                                # ! ^[a-z][a-z0-9-]{1,24}$  → stack and resource prefix
profile: nano|small|medium|large|xlarge = small
environment: dev|staging|production = production

aws:
  account: str?                          # 12 digits, defaults to the current credentials
  region: str?                           # defaults to the current credentials
  tags: {str: str} = {}
  permissions_boundary: arn?             # applied to every IAM role we create
  kms_key:                               # encryption at rest for data services
    kind: aws-managed | create | existing = aws-managed
    arn: str                             # kind=existing

n8n:
  version: str = stable                  # pin it, e.g. "2.37.1"; the runners image uses the same tag
  image: str = docker.n8n.io/n8nio/n8n
  timezone: str = UTC
  license:                                     # see 06-licensing.md
    edition: community | registered-community | business | enterprise = community
    activation_key: {secret: str, key: str?}   # → required unless community (or certificate)
    certificate: {secret: str, key: str?}      # offline licence (N8N_LICENSE_CERT), exclusive
    usage:                                     # → required for community editions
      purpose: internal-business | personal | non-commercial
             | service-for-third-parties | embedded-editor
      accept_sustainable_use_license: bool     # → must be true for community editions
      employees: int?                          # Business plan guidance (< 100)
  encryption_key:
    kind: generate | existing = generate       # generate → Secrets Manager, RETAIN on delete
    secret: str                                # kind=existing
  runners:
    mode: external | internal = external       # internal is deprecated in 2.x
    image: str = n8nio/runners
    max_concurrency: int = 5
  executions:
    timeout_seconds: int = -1
    max_age_hours: int = 336
    max_count: int = 10000
    save_on_success: all | none = all
    offload_manual_to_workers: bool = true     # queue topologies only
  public_api: bool = true
  metrics: bool = false                        # N8N_METRICS → /metrics (404 at ingress)
  env: {str: str} = {}                         # raw n8n variables
  secret_env: {str: {secret: str, key: str?}} = {}

topology:
  mode: single | queue | split | multi-main    # from profile
  main:    {replicas: int, cpu: int, memory: int}          # cpu units, MiB
  webhook: {min: int, max: int, cpu: int, memory: int, scale_on_cpu: int = 60}
  workers: {min: int, max: int, cpu: int, memory: int,
            concurrency: int = 10,
            scale_on: queue-depth | cpu = queue-depth,
            target: int = 20,                  # waiting jobs for a faster scale-out, or CPU %
            scale_in_minutes: int = 5,         # in-flight jobs fit on one worker less
            spot: bool}

runtime:
  platform: ecs | eks = ecs
  ecs:
    cluster: {kind: create} | {kind: existing, name: str}
    capacity: fargate | fargate-spot | ec2 = fargate
    architecture: arm64 | x86_64 = arm64
    ec2: {instance_type: str = m7g.large, min: int = 1, max: int = 4}   # capacity=ec2
    exec: bool = false                         # ECS Exec; turning it on later needs new tasks
  eks:
    cluster:
      kind: create | existing = create
      name: str                                # existing
      version: str = "1.34"                    # create
      compute: auto | managed-nodes | fargate = auto   # create; auto = EKS Auto Mode
      node_group: {instance_types: [str], min: int, max: int}  # managed-nodes
      admin_role_arns: [arn] = []
    namespace: str = n8n
    chart: {repository: str = oci://ghcr.io/n8n-io/n8n-helm-chart/n8n, version: str?}
    keda: install | existing | none = install
    load_balancer_controller: install | existing = install

network:
  vpc:
    # kind=create
    kind: create
    cidr: str = 10.80.0.0/16
    azs: 2 | 3 = 2
    nat: none | instance | single | per-az    # from profile; none → public-IP tasks
    endpoints: [s3, ecr, logs, secretsmanager, sts] = [s3]
    # kind=existing
    # kind: existing
    # id: vpc-...
    # availability_zones: [eu-west-1a, eu-west-1b]   # ! same order as the subnets
    # private_subnets: [subnet-...]           # ! one per AZ
    # public_subnets: [subnet-...]            # ! when ingress.public and no CloudFront-only origin

ingress:
  kind: cloudfront | alb                      # from profile: cloudfront (nano, small), alb (medium+)
  domain: str?                                # n8n.example.com; absent → default CloudFront/ALB name
  public: bool = true                         # false → internal ALB (kind=alb only)
  allowed_cidrs: [str] = [0.0.0.0/0]
  hosted_zone: {kind: lookup, name: str} | {kind: existing, id: str, name: str} | {kind: none}
  certificate: {kind: create} | {kind: existing, arn: str}      # create needs hosted_zone
  idle_timeout_seconds: int = 300
  cloudfront:                                 # kind=cloudfront
    origin: task | alb = task                 # task: no ALB, single main replica (07-finops.md)
    price_class: 100 | 200 | all = 100
    waf: {kind: none} | {kind: managed, rate_limit: int = 2000} | {kind: existing, arn: str}  # global scope

auth:
  mode: none | ip-allowlist | alb-cognito | alb-oidc | cloudfront-cognito = none
  cognito:                                    # alb-cognito, cloudfront-cognito
    kind: create | existing = create
    # create
    mfa: off | optional | required = optional
    self_signup: bool = false
    invite: [email] = []                      # initial users
    federation: {kind: none} | {kind: oidc, issuer: str, client_id: str, client_secret: {secret: str}}
                | {kind: saml, metadata_url: str}
    # existing (cloudfront-cognito: a client without secret, callback /_auth/callback)
    user_pool_id: str
    client_id: str
    domain: str                               # prefix, or the full hosted UI domain
  oidc:                                       # alb-oidc
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    user_info_endpoint: str
    client_id: str
    client_secret: {secret: str, key: str?}
    scope: str = openid email
  public_paths: [str] = []                    # added to the built-in bypass list
  session_hours: int = 12
  n8n_sso:                                    # n8n's own SSO, Business/Enterprise
    kind: none | oidc | saml = none
    discovery_endpoint: str                   # oidc; may reuse the created Cognito pool with `cognito`
    client_id: str
    client_secret: {secret: str}
    metadata_url: str                         # saml
    role_provisioning: disabled | instance_role | instance_and_project_roles = disabled

database:
  kind: sqlite | aurora-serverless | aurora | rds | external     # from profile
  # common to aurora-serverless, aurora, rds
  engine_version: str = "17"
  backup_retention_days: int = 7
  deletion_protection: bool = true            # default false when environment=dev
  # aurora-serverless
  min_acu: float = 0.5                        # 0 allowed (scale to zero)
  max_acu: float = 4
  readers: int = 0
  # aurora
  instance_class: str = db.r7g.large
  instances: int = 2
  io_optimized: bool = false
  # rds
  # instance_class: db.t4g.small
  multi_az: bool = false
  storage_gb: int = 20
  # external (alien or existing database)
  host: str
  port: int = 5432
  name: str = n8n
  schema: str = public
  credentials: {secret: str}                  # JSON with username/password
  ssl: disable | require | verify-full = verify-full
  security_group_id: str?                     # we add an ingress rule from the n8n tasks
  # all
  pool_size: int = 4

queue:
  kind: none | elasticache-serverless | elasticache | external   # from topology/profile
  engine: valkey | redis = valkey             # created kinds
  # elasticache
  node_type: str = cache.t4g.small
  replicas: int = 1
  # external
  host: str
  port: int = 6379
  tls: bool = true
  auth: {secret: str}?
  cluster: bool = false
  security_group_id: str?
  key_prefix: str = n8n                       # N8N_REDIS_KEY_PREFIX for shared Redis

storage:
  bucket:
    kind: none | create | existing = none
    name: str                                 # existing (create derives a name)
    prefix: str = ""                          # existing; n8n data under <prefix>
    kms_key_arn: str?
    binary_expiry_days: int = 30              # lifecycle on workflows/ only
  binary_data: database | filesystem | s3     # default: database (queue) / filesystem (single)
  execution_data: database | s3 = database
  exchange:                                   # S3 for workflows' heavy data exchange
    enabled: bool = false
    prefix: str = exchange/
    expiry_days: int?
  efs: auto | always | never = auto           # auto: when sqlite or filesystem

observability:
  log_level: error | warn | info | debug = info
  log_retention_days: int = 30
  container_insights: bool = false
  alarms: {email: [email] = []}               # alarms always exist; emails notify
  dashboard:                                  # one CloudWatch dashboard per deployment
    enabled: bool = true                      # nano profile: false
    period_minutes: 1 | 5 | 15 | 60 = 5
    range: str = 3h                           # <n>m, <n>h, <n>d or <n>w
  prometheus: none | cloudwatch | amp = none   # cloudwatch: agent sidecar on main; amp later

backup:
  kind: none | aws-backup = none
  retention_days: int = 35
  cross_region: str?

finops:                                       # see 07-finops.md
  budget: {monthly_usd: float?, alert_emails: [email] = []}
  anomaly_detection: bool = false
  schedule: {kind: always-on | office-hours = always-on, timezone: str = UTC,
             weekdays: str = MON-FRI, start: str = "08:00", stop: str = "19:00"}
  max_monthly_usd: float?                     # plan fails above this estimate
```

## 4.4 Pydantic sketch

```python
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecretRef(Strict):
    secret: str
    key: str | None = None


class _ManagedPostgres(Strict):
    engine_version: str = "17"
    backup_retention_days: int = Field(7, ge=1, le=35)
    deletion_protection: bool = True
    pool_size: int = Field(4, ge=1)


class AuroraServerless(_ManagedPostgres):
    kind: Literal["aurora-serverless"]
    min_acu: float = Field(0.5, ge=0, le=256)
    max_acu: float = Field(4, ge=1, le=256)
    readers: int = Field(0, ge=0, le=15)


class ExternalPostgres(Strict):
    kind: Literal["external"]
    host: str
    port: int = 5432
    name: str = "n8n"
    schema_: str = Field("public", alias="schema")
    credentials: SecretRef
    ssl: Literal["disable", "require", "verify-full"] = "verify-full"
    security_group_id: str | None = None
    pool_size: int = Field(4, ge=1)


Database = Annotated[
    Sqlite | AuroraServerless | AuroraProvisioned | Rds | ExternalPostgres,
    Field(discriminator="kind"),
]
```

Cross-field rules (§3.5 of the options study) live in a single `@model_validator(mode="after")` on the
root `Deployment` model. They return all errors at once, each with a path (`database.kind`) and a hint.
`profile` defaults are applied in a `mode="before"` validator by deep-merging the profile dictionary
under the user's data. That keeps "explicit beats profile" trivially true, and the plan can report where
each value came from.

The JSON Schema is exported from the models (`n8n-on-aws schema > schema/v1.json`). Samples start with
`# yaml-language-server: $schema=...` for editor completion.

## 4.5 What the grammar compiles to (sketch)

| Section | ECS | EKS |
|---|---|---|
| `topology.*` | one ECS service per role, task def with n8n + runners containers | Helm values: `main`, `webhook`, `worker` blocks; runner sidecars |
| `workers.scale_on: queue-depth` | CloudWatch agent sidecar on main scrapes n8n's queue gauges into the `n8n-on-aws` namespace; step scaling out on waiting jobs (from zero workers), in when the jobs in flight fit on one worker less, plus a CPU policy that never scales in | KEDA `redis` scaler |
| `database`, `queue`, `storage.bucket` | shared `<name>-data` stack | same |
| `auth.alb-*` | ALB listener rules: bypass list → forward; default → authenticate then forward | ALB Ingress annotations (`alb.ingress.kubernetes.io/auth-*`) |
| `auth.cloudfront-cognito` | Lambda@Edge viewer-request function in the `<name>-edge` stack (us-east-1); Cognito pool and public client in `<name>-n8n`, which writes their IDs to an SSM parameter in us-east-1; one CloudFront behavior without the function per public path | same |
| secrets | ECS `secrets:` from Secrets Manager | External Secrets Operator or Secrets Store CSI, via Pod Identity |
