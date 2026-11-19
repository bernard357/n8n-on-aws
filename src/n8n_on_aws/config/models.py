"""Pydantic models of the ``n8n-on-aws/v1`` configuration file.

Every resource that the tool may either create or reuse is a union discriminated by
``kind``. Keys that belong to another kind are rejected, so a typo never passes
silently. Cross-field rules (topology, licence, FinOps) live in ``rules.py``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,24}$")]
Email = Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]

Profile = Literal["nano", "small", "medium", "large", "xlarge"]
Environment = Literal["dev", "staging", "production"]
Edition = Literal["community", "registered-community", "business", "enterprise"]
Purpose = Literal[
    "internal-business",
    "personal",
    "non-commercial",
    "service-for-third-parties",
    "embedded-editor",
]
TopologyMode = Literal["single", "queue", "split", "multi-main"]


class Strict(BaseModel):
    """Base model: unknown keys are errors, aliases and names both accepted."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SecretRef(Strict):
    """A reference to an AWS Secrets Manager secret, never the value itself."""

    secret: str = Field(min_length=1, description="Secret name or ARN")
    key: str | None = Field(None, description="JSON key inside the secret")


# ----------------------------------------------------------------------- aws


class AwsManagedKey(Strict):
    kind: Literal["aws-managed"] = "aws-managed"


class CreatedKey(Strict):
    kind: Literal["create"]


class ExistingKey(Strict):
    kind: Literal["existing"]
    arn: str = Field(pattern=r"^arn:aws[a-z-]*:kms:")


KmsKey = Annotated[
    AwsManagedKey | CreatedKey | ExistingKey, Field(discriminator="kind")
]


class Aws(Strict):
    account: str | None = Field(None, pattern=r"^\d{12}$")
    region: str | None = Field(None, pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    tags: dict[str, str] = Field(default_factory=dict)
    permissions_boundary: str | None = Field(None, pattern=r"^arn:aws[a-z-]*:iam::")
    kms_key: KmsKey = Field(default_factory=AwsManagedKey)


# ----------------------------------------------------------------------- n8n


class Usage(Strict):
    """Declared use of the instance, checked against n8n's licence terms."""

    purpose: Purpose
    accept_sustainable_use_license: bool = False
    employees: int | None = Field(None, ge=1)


class License(Strict):
    edition: Edition = "community"
    activation_key: SecretRef | None = None
    certificate: SecretRef | None = None
    usage: Usage | None = None


class GeneratedEncryptionKey(Strict):
    kind: Literal["generate"] = "generate"


class ExistingEncryptionKey(Strict):
    kind: Literal["existing"]
    secret: str = Field(min_length=1)


EncryptionKey = Annotated[
    GeneratedEncryptionKey | ExistingEncryptionKey, Field(discriminator="kind")
]


class Runners(Strict):
    mode: Literal["external", "internal"] = "external"
    image: str = "n8nio/runners"
    max_concurrency: int = Field(5, ge=1)


class Executions(Strict):
    timeout_seconds: int = Field(-1, ge=-1)
    max_age_hours: int = Field(336, ge=1)
    max_count: int = Field(10000, ge=0)
    save_on_success: Literal["all", "none"] = "all"
    offload_manual_to_workers: bool = True


class N8n(Strict):
    version: str = "stable"
    image: str = "docker.n8n.io/n8nio/n8n"
    timezone: str = "UTC"
    license: License = Field(default_factory=License)
    encryption_key: EncryptionKey = Field(default_factory=GeneratedEncryptionKey)
    runners: Runners = Field(default_factory=Runners)
    executions: Executions = Field(default_factory=Executions)
    public_api: bool = True
    metrics: bool = False
    telemetry: bool = Field(
        False, description="send n8n's usage telemetry (N8N_DIAGNOSTICS_ENABLED)"
    )
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, SecretRef] = Field(default_factory=dict)


# ------------------------------------------------------------------ topology


class Main(Strict):
    replicas: int = Field(1, ge=1, le=10)
    cpu: int = Field(512, description="CPU units (1024 = 1 vCPU)")
    memory: int = Field(1024, description="MiB")


class Webhook(Strict):
    min: int = Field(1, ge=1)
    max: int = Field(4, ge=1)
    cpu: int = 512
    memory: int = 1024
    scale_on_cpu: int = Field(60, ge=10, le=95)

    @model_validator(mode="after")
    def _bounds(self) -> Webhook:
        if self.max < self.min:
            raise ValueError("max must be greater than or equal to min")
        return self


class Workers(Strict):
    min: int = Field(1, ge=0)
    max: int = Field(4, ge=1)
    cpu: int = 1024
    memory: int = 2048
    concurrency: int = Field(10, ge=1, le=100)
    scale_on: Literal["queue-depth", "cpu"] = "queue-depth"
    target: int = Field(
        20, ge=1, description="waiting jobs that add a worker, or CPU % with cpu"
    )
    scale_in_minutes: int = Field(
        5,
        ge=1,
        le=60,
        description="minutes the remaining jobs must fit on one worker less "
        "before it is removed (queue-depth)",
    )
    spot: bool = False

    @model_validator(mode="after")
    def _bounds(self) -> Workers:
        if self.max < self.min:
            raise ValueError("max must be greater than or equal to min")
        return self


class Topology(Strict):
    mode: TopologyMode = "single"
    main: Main = Field(default_factory=Main)
    webhook: Webhook = Field(default_factory=Webhook)
    workers: Workers = Field(default_factory=Workers)


# ------------------------------------------------------------------- runtime


class CreatedEcsCluster(Strict):
    kind: Literal["create"] = "create"


class ExistingEcsCluster(Strict):
    kind: Literal["existing"]
    name: str = Field(min_length=1)


EcsCluster = Annotated[
    CreatedEcsCluster | ExistingEcsCluster, Field(discriminator="kind")
]


class Ec2Capacity(Strict):
    instance_types: list[str] = Field(
        default_factory=lambda: ["m7g.large"],
        min_length=1,
        description="the first is sized against; more types diversify Spot",
    )
    min: int = Field(1, ge=0, description="on-demand instances, always running")
    max: int = Field(4, ge=1)
    spot_max: int = Field(
        4, ge=1, description="Spot instances for workers, with topology.workers.spot"
    )
    root_volume_gib: int = Field(30, ge=30, le=1000)

    @model_validator(mode="after")
    def _bounds(self) -> Ec2Capacity:
        if self.max < self.min:
            raise ValueError("max must be greater than or equal to min")
        return self


class Ecs(Strict):
    cluster: EcsCluster = Field(default_factory=CreatedEcsCluster)
    capacity: Literal["fargate", "fargate-spot", "ec2"] = "fargate"
    architecture: Literal["arm64", "x86_64"] = "arm64"
    ec2: Ec2Capacity = Field(default_factory=Ec2Capacity)
    exec: bool = False


class NodeGroup(Strict):
    instance_types: list[str] = Field(default_factory=lambda: ["m7g.large"])
    min: int = Field(2, ge=0)
    max: int = Field(6, ge=1)


class CreatedEksCluster(Strict):
    kind: Literal["create"] = "create"
    version: str = Field("1.34", pattern=r"^1\.\d+$")
    compute: Literal["auto", "managed-nodes", "fargate"] = "auto"
    node_group: NodeGroup = Field(default_factory=NodeGroup)
    admin_role_arns: list[str] = Field(default_factory=list)


class ExistingEksCluster(Strict):
    kind: Literal["existing"]
    name: str = Field(min_length=1)
    admin_role_arns: list[str] = Field(default_factory=list)


EksCluster = Annotated[
    CreatedEksCluster | ExistingEksCluster, Field(discriminator="kind")
]


class Chart(Strict):
    repository: str = "oci://ghcr.io/n8n-io/n8n-helm-chart/n8n"
    version: str | None = None


class Eks(Strict):
    cluster: EksCluster = Field(default_factory=CreatedEksCluster)
    namespace: str = Field("n8n", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    chart: Chart = Field(default_factory=Chart)
    keda: Literal["install", "existing", "none"] = "install"
    load_balancer_controller: Literal["install", "existing"] = "install"


class Runtime(Strict):
    platform: Literal["ecs", "eks"] = "ecs"
    ecs: Ecs = Field(default_factory=Ecs)
    eks: Eks = Field(default_factory=Eks)


# ------------------------------------------------------------------- network

Endpoint = Literal["s3", "ecr", "logs", "secretsmanager", "sts"]
Nat = Literal["none", "instance", "single", "per-az"]


class CreatedVpc(Strict):
    kind: Literal["create"] = "create"
    cidr: str = Field("10.80.0.0/16", pattern=r"^\d+\.\d+\.\d+\.\d+/\d+$")
    azs: Literal[2, 3] = 2
    nat: Nat = "single"
    endpoints: list[Endpoint] = Field(default_factory=lambda: ["s3"])  # type: ignore[arg-type]


class ExistingVpc(Strict):
    kind: Literal["existing"]
    id: str = Field(pattern=r"^vpc-[0-9a-f]+$")
    availability_zones: list[str] = Field(
        min_length=2, description="AZs of the subnets, in the same order"
    )
    private_subnets: list[str] = Field(min_length=2)
    public_subnets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_subnet_per_az(self) -> ExistingVpc:
        azs = len(self.availability_zones)
        for label, subnets in (
            ("private_subnets", self.private_subnets),
            ("public_subnets", self.public_subnets),
        ):
            if subnets and len(subnets) % azs:
                raise ValueError(f"{label} must list one subnet per availability zone")
        return self


Vpc = Annotated[CreatedVpc | ExistingVpc, Field(discriminator="kind")]


class Network(Strict):
    vpc: Vpc = Field(default_factory=CreatedVpc)


# ------------------------------------------------------------------- ingress


class LookupZone(Strict):
    kind: Literal["lookup"]
    name: str


class ExistingZone(Strict):
    kind: Literal["existing"]
    id: str = Field(pattern=r"^Z[A-Z0-9]+$")
    name: str


class NoZone(Strict):
    kind: Literal["none"] = "none"


HostedZone = Annotated[LookupZone | ExistingZone | NoZone, Field(discriminator="kind")]


class CreatedCertificate(Strict):
    kind: Literal["create"]


class ExistingCertificate(Strict):
    kind: Literal["existing"]
    arn: str = Field(pattern=r"^arn:aws[a-z-]*:acm:")


class NoCertificate(Strict):
    kind: Literal["none"] = "none"


Certificate = Annotated[
    CreatedCertificate | ExistingCertificate | NoCertificate,
    Field(discriminator="kind"),
]


class NoWaf(Strict):
    kind: Literal["none"] = "none"


class ManagedWaf(Strict):
    kind: Literal["managed"]
    rate_limit: int = Field(2000, ge=100)


class ExistingWaf(Strict):
    kind: Literal["existing"]
    arn: str = Field(pattern=r"^arn:aws[a-z-]*:wafv2:")


Waf = Annotated[NoWaf | ManagedWaf | ExistingWaf, Field(discriminator="kind")]


class CloudFront(Strict):
    origin: Literal["task", "alb"] = "task"
    price_class: Literal["100", "200", "all"] = "100"
    waf: Waf = Field(default_factory=NoWaf)


class Ingress(Strict):
    kind: Literal["cloudfront", "alb"] = "cloudfront"
    domain: str | None = Field(
        None, pattern=r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?\.)+[a-z]{2,}$"
    )
    public: bool = True
    allowed_cidrs: list[str] = Field(default_factory=lambda: ["0.0.0.0/0"])
    hosted_zone: HostedZone = Field(default_factory=NoZone)
    certificate: Certificate = Field(default_factory=NoCertificate)
    idle_timeout_seconds: int = Field(300, ge=60, le=4000)
    cloudfront: CloudFront = Field(default_factory=CloudFront)
    waf: Waf = Field(default_factory=NoWaf, description="WAF on the ALB")


# ---------------------------------------------------------------------- auth


class NoFederation(Strict):
    kind: Literal["none"] = "none"


class OidcFederation(Strict):
    kind: Literal["oidc"]
    issuer: str
    client_id: str
    client_secret: SecretRef


class SamlFederation(Strict):
    kind: Literal["saml"]
    metadata_url: str


Federation = Annotated[
    NoFederation | OidcFederation | SamlFederation, Field(discriminator="kind")
]


class CreatedCognito(Strict):
    kind: Literal["create"] = "create"
    mfa: Literal["off", "optional", "required"] = "optional"
    self_signup: bool = False
    invite: list[Email] = Field(default_factory=list)
    federation: Federation = Field(default_factory=NoFederation)


class ExistingCognito(Strict):
    kind: Literal["existing"]
    user_pool_id: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d_[A-Za-z0-9]+$")
    client_id: str
    domain: str


Cognito = Annotated[CreatedCognito | ExistingCognito, Field(discriminator="kind")]


class Oidc(Strict):
    """An OIDC identity provider for the ALB authenticate-oidc action."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    user_info_endpoint: str
    client_id: str
    client_secret: SecretRef
    scope: str = "openid email"


class NoSso(Strict):
    kind: Literal["none"] = "none"


class OidcSso(Strict):
    kind: Literal["oidc"]
    discovery_endpoint: str
    client_id: str
    client_secret: SecretRef
    role_provisioning: Literal[
        "disabled", "instance_role", "instance_and_project_roles"
    ] = "disabled"


class SamlSso(Strict):
    kind: Literal["saml"]
    metadata_url: str
    role_provisioning: Literal[
        "disabled", "instance_role", "instance_and_project_roles"
    ] = "disabled"


N8nSso = Annotated[NoSso | OidcSso | SamlSso, Field(discriminator="kind")]

AuthMode = Literal[
    "none", "ip-allowlist", "alb-cognito", "alb-oidc", "cloudfront-cognito"
]


class Auth(Strict):
    mode: AuthMode = "none"
    cognito: Cognito = Field(default_factory=CreatedCognito)
    oidc: Oidc | None = None
    public_paths: list[str] = Field(default_factory=list)
    session_hours: int = Field(12, ge=1, le=168)
    n8n_sso: N8nSso = Field(default_factory=NoSso)


# ------------------------------------------------------------------ database


class _ManagedPostgres(Strict):
    engine_version: str = "17"
    backup_retention_days: int = Field(7, ge=1, le=35)
    deletion_protection: bool = True
    pool_size: int = Field(4, ge=1)


class Sqlite(Strict):
    kind: Literal["sqlite"]


class AuroraServerless(_ManagedPostgres):
    kind: Literal["aurora-serverless"]
    min_acu: float = Field(0.5, ge=0, le=256)
    max_acu: float = Field(4, ge=1, le=256)
    readers: int = Field(0, ge=0, le=15)


class AuroraProvisioned(_ManagedPostgres):
    kind: Literal["aurora"]
    instance_class: str = Field("db.r7g.large", pattern=r"^db\.")
    instances: int = Field(2, ge=1, le=16)
    io_optimized: bool = False


class Rds(_ManagedPostgres):
    kind: Literal["rds"]
    instance_class: str = Field("db.t4g.small", pattern=r"^db\.")
    multi_az: bool = False
    storage_gb: int = Field(20, ge=20)


class ExternalPostgres(Strict):
    kind: Literal["external"]
    host: str
    port: int = 5432
    name: str = "n8n"
    db_schema: str = Field("public", alias="schema")
    credentials: SecretRef
    ssl: Literal["disable", "require", "verify-full"] = "verify-full"
    security_group_id: str | None = Field(None, pattern=r"^sg-[0-9a-f]+$")
    pool_size: int = Field(4, ge=1)


Database = Annotated[
    Sqlite | AuroraServerless | AuroraProvisioned | Rds | ExternalPostgres,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------- queue


class NoQueue(Strict):
    kind: Literal["none"] = "none"


class ElastiCacheServerless(Strict):
    kind: Literal["elasticache-serverless"]
    engine: Literal["valkey", "redis"] = "valkey"
    key_prefix: str = "n8n"


class ElastiCacheNodes(Strict):
    kind: Literal["elasticache"]
    engine: Literal["valkey", "redis"] = "valkey"
    node_type: str = Field("cache.t4g.small", pattern=r"^cache\.")
    replicas: int = Field(1, ge=0, le=5)
    key_prefix: str = "n8n"


class ExternalRedis(Strict):
    kind: Literal["external"]
    host: str
    port: int = 6379
    tls: bool = True
    auth: SecretRef | None = None
    cluster: bool = False
    security_group_id: str | None = Field(None, pattern=r"^sg-[0-9a-f]+$")
    key_prefix: str = "n8n"


Queue = Annotated[
    NoQueue | ElastiCacheServerless | ElastiCacheNodes | ExternalRedis,
    Field(discriminator="kind"),
]


# ------------------------------------------------------------------- storage


class NoBucket(Strict):
    kind: Literal["none"] = "none"


class CreatedBucket(Strict):
    kind: Literal["create"]
    binary_expiry_days: int | None = Field(30, ge=1)


class ExistingBucket(Strict):
    kind: Literal["existing"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    prefix: str = ""
    kms_key_arn: str | None = Field(None, pattern=r"^arn:aws[a-z-]*:kms:")


Bucket = Annotated[
    NoBucket | CreatedBucket | ExistingBucket, Field(discriminator="kind")
]


class Exchange(Strict):
    enabled: bool = False
    prefix: str = "exchange/"
    expiry_days: int | None = Field(None, ge=1)


class Storage(Strict):
    bucket: Bucket = Field(default_factory=NoBucket)
    binary_data: Literal["database", "filesystem", "s3"] | None = None
    execution_data: Literal["database", "s3"] = "database"
    exchange: Exchange = Field(default_factory=Exchange)
    efs: Literal["auto", "always", "never"] = "auto"


# ----------------------------------------------------- observability, backup


class Alarms(Strict):
    email: list[Email] = Field(default_factory=list)


class Dashboard(Strict):
    """One CloudWatch dashboard generated from the deployed resources."""

    enabled: bool = True
    period_minutes: Literal[1, 5, 15, 60] = Field(
        5, description="Default metric period of the widgets"
    )
    range: str = Field(
        "3h",
        pattern=r"^[1-9]\d*[mhdw]$",
        description="Time range shown when the dashboard opens, e.g. 3h, 7d",
    )


class Observability(Strict):
    log_level: Literal["error", "warn", "info", "debug"] = "info"
    log_retention_days: int = Field(30, ge=0, description="0 keeps logs forever")
    container_insights: bool = False
    alarms: Alarms = Field(default_factory=Alarms)
    dashboard: Dashboard = Field(default_factory=Dashboard)
    prometheus: Literal["none", "cloudwatch", "amp"] = "none"


class Backup(Strict):
    kind: Literal["none", "aws-backup"] = "none"
    retention_days: int = Field(35, ge=1)
    cross_region: str | None = Field(None, pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")


# -------------------------------------------------------------------- finops


class Budget(Strict):
    monthly_usd: float | None = Field(None, gt=0)
    alert_emails: list[Email] = Field(default_factory=list)


class Schedule(Strict):
    kind: Literal["always-on", "office-hours"] = "always-on"
    timezone: str = "UTC"
    weekdays: str = "MON-FRI"
    start: str = Field("08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    stop: str = Field("19:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class TrafficUsage(Strict):
    """Traffic assumptions of the cost estimate; unset values come from the profile."""

    requests_millions: float | None = Field(
        None, ge=0, description="HTTP requests a month, in millions"
    )
    data_out_gb: float | None = Field(
        None, ge=0, description="GB a month sent to users and by workflows"
    )
    data_gb: float | None = Field(
        None, ge=0, description="database, bucket or EFS data, in GB"
    )
    logs_gb: float | None = Field(None, ge=0, description="logs ingested a month, GB")
    average_acu: float | None = Field(
        None, ge=0, description="average Aurora Serverless capacity"
    )
    average_workers: float | None = Field(
        None, ge=0, description="average running workers (queue modes)"
    )


class FinOps(Strict):
    budget: Budget = Field(default_factory=Budget)
    anomaly_detection: bool = False
    schedule: Schedule = Field(default_factory=Schedule)
    max_monthly_usd: float | None = Field(None, gt=0)
    usage: TrafficUsage = Field(default_factory=TrafficUsage)


# ---------------------------------------------------------------------- root


class Deployment(Strict):
    """The whole configuration file, after profile defaults are merged in."""

    api_version: Literal["n8n-on-aws/v1"] = Field(alias="apiVersion")
    name: Name
    profile: Profile = "small"
    environment: Environment = "production"
    aws: Aws = Field(default_factory=Aws)
    n8n: N8n = Field(default_factory=N8n)
    topology: Topology = Field(default_factory=Topology)
    runtime: Runtime = Field(default_factory=Runtime)
    network: Network = Field(default_factory=Network)
    ingress: Ingress = Field(default_factory=Ingress)
    auth: Auth = Field(default_factory=Auth)
    database: Database = Field(
        default_factory=lambda: AuroraServerless(kind="aurora-serverless")
    )
    queue: Queue = Field(default_factory=NoQueue)
    storage: Storage = Field(default_factory=Storage)
    observability: Observability = Field(default_factory=Observability)
    backup: Backup = Field(default_factory=Backup)
    finops: FinOps = Field(default_factory=FinOps)

    @property
    def is_queued(self) -> bool:
        """True when n8n runs in queue mode (workers pull from Redis)."""
        return self.topology.mode != "single"

    @property
    def binary_mode(self) -> str:
        """The effective binary data mode, with the topology-dependent default."""
        if self.storage.binary_data:
            return self.storage.binary_data
        return "filesystem" if self.database.kind == "sqlite" else "database"

    @property
    def scales_on_queue(self) -> bool:
        """True when workers scale on the number of waiting jobs."""
        return self.is_queued and self.topology.workers.scale_on == "queue-depth"

    @property
    def collects_metrics(self) -> bool:
        """True when a CloudWatch agent scrapes n8n's Prometheus metrics."""
        return self.observability.prometheus == "cloudwatch" or self.scales_on_queue

    @property
    def has_alb(self) -> bool:
        """True when an Application Load Balancer is part of the deployment."""
        return self.ingress.kind == "alb" or self.ingress.cloudfront.origin == "alb"

    @property
    def needs_efs(self) -> bool:
        """True when n8n's user folder must live on EFS."""
        if self.storage.efs == "always":
            return True
        if self.storage.efs == "never":
            return False
        return self.database.kind == "sqlite" or self.binary_mode == "filesystem"
