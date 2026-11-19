"""Cross-field rules: n8n topology, licence enforcement and FinOps.

Each rule has a stable code (``T`` topology and AWS, ``L`` licence, ``F`` FinOps) so
that docs, tests and users can refer to it. All findings are collected; nothing stops
at the first problem.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import instances as ec2
from . import licensing as lic
from .models import Deployment

Level = Literal["error", "warning", "info"]


class Finding(BaseModel):
    """One result of validation, with where it is and how to fix it."""

    model_config = ConfigDict(frozen=True)

    level: Level
    code: str
    path: str
    message: str
    hint: str | None = None


Rule = Callable[[Deployment], Iterator[Finding]]
RULES: list[Rule] = []


def rule(func: Rule) -> Rule:
    """Register a rule function."""
    RULES.append(func)
    return func


def _error(code: str, path: str, message: str, hint: str | None = None) -> Finding:
    return Finding(level="error", code=code, path=path, message=message, hint=hint)


def _warn(code: str, path: str, message: str, hint: str | None = None) -> Finding:
    return Finding(level="warning", code=code, path=path, message=message, hint=hint)


def _info(code: str, path: str, message: str, hint: str | None = None) -> Finding:
    return Finding(level="info", code=code, path=path, message=message, hint=hint)


# Valid Fargate CPU (units) → memory (MiB) pairs.
_FARGATE_MEMORY: dict[int, range] = {
    256: range(512, 2048 + 1, 512),
    512: range(1024, 4096 + 1, 1024),
    1024: range(2048, 8192 + 1, 1024),
    2048: range(4096, 16384 + 1, 1024),
    4096: range(8192, 30720 + 1, 1024),
    8192: range(16384, 61440 + 1, 4096),
    16384: range(32768, 122880 + 1, 8192),
}


def fargate_size_ok(cpu: int, memory: int) -> bool:
    """True when ``cpu``/``memory`` is a valid Fargate task size."""
    allowed = _FARGATE_MEMORY.get(cpu)
    return allowed is not None and memory in allowed


# ------------------------------------------------------------------ topology


@rule
def topology_needs_shared_state(d: Deployment) -> Iterator[Finding]:
    if not d.is_queued:
        return
    if d.database.kind == "sqlite":
        yield _error(
            "T1",
            "database.kind",
            f"topology '{d.topology.mode}' needs PostgreSQL; SQLite cannot be shared",
            "use aurora-serverless, aurora, rds or external",
        )
    if d.queue.kind == "none":
        yield _error(
            "T1",
            "queue.kind",
            f"topology '{d.topology.mode}' needs a Redis-compatible queue",
            "use elasticache-serverless, elasticache or external",
        )


@rule
def single_topology(d: Deployment) -> Iterator[Finding]:
    if d.is_queued:
        return
    if d.queue.kind != "none":
        yield _warn(
            "T2",
            "queue.kind",
            "a queue is configured but topology 'single' does not use it",
            "remove the queue block, or set topology.mode to queue",
        )
    if d.topology.main.replicas != 1:
        yield _error(
            "T2",
            "topology.main.replicas",
            "topology 'single' runs exactly one n8n process",
            "use topology.mode multi-main for several main replicas",
        )


@rule
def multi_main_replicas(d: Deployment) -> Iterator[Finding]:
    if d.topology.mode == "multi-main" and d.topology.main.replicas < 2:
        yield _error(
            "T3",
            "topology.main.replicas",
            "multi-main needs at least 2 main replicas",
        )
    if d.topology.mode in ("queue", "split") and d.topology.main.replicas > 1:
        yield _error(
            "T3",
            "topology.main.replicas",
            "several main replicas need topology.mode multi-main",
        )


@rule
def local_files_need_single(d: Deployment) -> Iterator[Finding]:
    if d.binary_mode == "filesystem" and d.is_queued:
        yield _error(
            "T4",
            "storage.binary_data",
            "filesystem binary data cannot be shared between n8n processes",
            "use database or s3",
        )
    if d.needs_efs and d.runtime.platform == "eks":
        yield _error(
            "T4",
            "database.kind" if d.database.kind == "sqlite" else "storage.binary_data",
            "SQLite and filesystem binary data on EFS are supported on ECS only",
            "use PostgreSQL and database or s3 binary data on EKS",
        )
    if d.needs_efs and d.storage.efs == "never":
        yield _error(
            "T4",
            "storage.efs",
            "SQLite or filesystem binary data need EFS, or data is lost on restart",
        )


@rule
def eks_needs_domain(d: Deployment) -> Iterator[Finding]:
    if d.runtime.platform == "eks" and not d.ingress.domain:
        yield _error(
            "T5",
            "ingress.domain",
            "EKS needs a domain: the ALB hostname is not known at synthesis time",
        )
    if d.runtime.platform == "eks" and d.ingress.kind == "cloudfront":
        if d.ingress.cloudfront.origin == "task":
            yield _error(
                "T5",
                "ingress.cloudfront.origin",
                "the 'task' origin is an ECS feature",
                "use origin alb on EKS",
            )


@rule
def auth_wrappers(d: Deployment) -> Iterator[Finding]:
    mode = d.auth.mode
    if mode in ("alb-cognito", "alb-oidc"):
        if d.ingress.kind != "alb":
            yield _error(
                "T6",
                "auth.mode",
                f"{mode} is enforced by an internet-facing ALB listener on HTTPS",
                "use cloudfront-cognito with CloudFront, or ingress.kind alb",
            )
        if not d.ingress.domain:
            yield _error(
                "T6",
                "ingress.domain",
                f"{mode} needs HTTPS on a domain of yours",
            )
    if mode == "alb-oidc" and d.auth.oidc is None:
        yield _error("T6", "auth.oidc", "alb-oidc needs an oidc block")
    if mode == "cloudfront-cognito" and d.ingress.kind != "cloudfront":
        yield _error(
            "T7",
            "auth.mode",
            "cloudfront-cognito needs ingress.kind cloudfront",
            "use alb-cognito with an ALB",
        )
    if mode == "cloudfront-cognito" and not d.ingress.domain:
        yield _error(
            "T7",
            "ingress.domain",
            "cloudfront-cognito needs a domain of yours for the Cognito callback",
        )
    if mode == "cloudfront-cognito" and len(d.auth.public_paths) > 10:
        yield _error(
            "T7",
            "auth.public_paths",
            "each public path is a CloudFront behavior; keep at most 10 extra paths "
            "(CloudFront allows 25 behaviors)",
        )
    if mode == "ip-allowlist" and "0.0.0.0/0" in d.ingress.allowed_cidrs:
        yield _error(
            "T6",
            "ingress.allowed_cidrs",
            "ip-allowlist needs ingress.allowed_cidrs narrower than 0.0.0.0/0",
        )
    edge_waf = d.ingress.cloudfront.waf
    if d.ingress.kind == "cloudfront" and edge_waf.kind == "existing":
        if ":global/" not in edge_waf.arn:
            yield _error(
                "T6",
                "ingress.cloudfront.waf.arn",
                "CloudFront only uses web ACLs of scope CLOUDFRONT "
                "(arn:aws:wafv2:us-east-1:...:global/webacl/...)",
            )
        if mode == "ip-allowlist":
            yield _error(
                "T6",
                "auth.mode",
                "ip-allowlist adds rules to a web ACL we create; it cannot change "
                "an existing one",
                "use waf kind managed or none, or put the allow-list in your web ACL",
            )
    certificate = d.ingress.certificate
    if (
        d.ingress.kind == "cloudfront"
        and certificate.kind == "existing"
        and ":us-east-1:" not in certificate.arn
    ):
        yield _error(
            "T6",
            "ingress.certificate.arn",
            "CloudFront only uses ACM certificates from us-east-1",
        )


@rule
def task_origin(d: Deployment) -> Iterator[Finding]:
    if d.ingress.kind != "cloudfront" or d.ingress.cloudfront.origin != "task":
        return
    if d.topology.mode not in ("single", "queue"):
        yield _error(
            "T8",
            "ingress.cloudfront.origin",
            f"the 'task' origin cannot route topology '{d.topology.mode}'",
            "use ingress.cloudfront.origin alb",
        )
    yield _info(
        "T8",
        "ingress.cloudfront.origin",
        "the hop from CloudFront to the task is plain HTTP, restricted to "
        "CloudFront's address ranges",
        "use origin alb if traffic must be encrypted end to end",
    )


@rule
def ingress_consistency(d: Deployment) -> Iterator[Finding]:
    ingress = d.ingress
    if not ingress.public and ingress.kind != "alb":
        yield _error(
            "T9", "ingress.public", "a private n8n needs ingress.kind alb (internal)"
        )
    if ingress.certificate.kind == "create":
        if not ingress.domain:
            yield _error(
                "T9", "ingress.certificate", "a created certificate needs a domain"
            )
        if ingress.hosted_zone.kind == "none":
            yield _error(
                "T9",
                "ingress.hosted_zone",
                "a created certificate is validated through a Route 53 zone",
                "set hosted_zone, or bring an existing certificate",
            )
    if ingress.domain and ingress.certificate.kind == "none":
        yield _error(
            "T9",
            "ingress.certificate",
            "a domain needs a certificate",
            "use {kind: create} with a hosted zone, or {kind: existing, arn: ...}",
        )
    if ingress.domain and ingress.hosted_zone.kind != "none":
        zone = ingress.hosted_zone.name.rstrip(".")
        if not (ingress.domain == zone or ingress.domain.endswith("." + zone)):
            yield _error(
                "T9",
                "ingress.hosted_zone.name",
                f"domain {ingress.domain} is not in zone {zone}",
            )
    if not ingress.domain:
        yield _warn(
            "T9",
            "ingress.domain",
            "no domain: n8n is served on the AWS-generated host name",
            "set ingress.domain for a stable URL and webhook addresses",
        )


@rule
def existing_vpc_subnets(d: Deployment) -> Iterator[Finding]:
    vpc = d.network.vpc
    if vpc.kind != "existing":
        return
    needs_public = (d.ingress.kind == "alb" and d.ingress.public) or (
        d.ingress.kind == "cloudfront" and d.ingress.cloudfront.origin == "task"
    )
    if needs_public and len(vpc.public_subnets) < 2:
        yield _error(
            "T10",
            "network.vpc.public_subnets",
            "this ingress needs at least 2 public subnets",
        )


@rule
def fargate_sizes(d: Deployment) -> Iterator[Finding]:
    if d.runtime.platform != "ecs" or d.runtime.ecs.capacity == "ec2":
        return
    sizes: list[tuple[str, int, int]] = [
        ("topology.main", d.topology.main.cpu, d.topology.main.memory)
    ]
    if d.topology.mode in ("split", "multi-main"):
        sizes.append(
            ("topology.webhook", d.topology.webhook.cpu, d.topology.webhook.memory)
        )
    if d.is_queued:
        sizes.append(
            ("topology.workers", d.topology.workers.cpu, d.topology.workers.memory)
        )
    for path, cpu, memory in sizes:
        if not fargate_size_ok(cpu, memory):
            yield _error(
                "T11",
                path,
                f"{cpu} CPU units with {memory} MiB is not a valid Fargate size",
                "see the Fargate task size table, e.g. 512/1024, 1024/2048",
            )


@rule
def ec2_capacity(d: Deployment) -> Iterator[Finding]:
    """ECS on EC2: what awsvpc tasks on an Auto Scaling group can and cannot do."""
    if d.runtime.platform != "ecs" or d.runtime.ecs.capacity != "ec2":
        return
    spec = d.runtime.ecs.ec2
    if d.runtime.ecs.cluster.kind == "existing":
        yield _error(
            "T17",
            "runtime.ecs.cluster",
            "EC2 capacity is added to a cluster this deployment creates",
            "use cluster kind create, or capacity fargate on an existing cluster",
        )
    if d.ingress.kind == "cloudfront" and d.ingress.cloudfront.origin == "task":
        yield _error(
            "T17",
            "ingress.cloudfront.origin",
            "tasks on EC2 get no public IP in awsvpc mode, so CloudFront cannot "
            "reach them directly",
            "use ingress.cloudfront.origin alb, or ingress kind alb",
        )
    if d.network.vpc.kind == "create" and d.network.vpc.nat == "none":
        yield _error(
            "T17",
            "network.vpc.nat",
            "tasks on EC2 get no public IP in awsvpc mode, so they need a NAT "
            "to reach the internet",
            "use nat: instance, single or per-az",
        )
    arm = d.runtime.ecs.architecture == "arm64"
    for index, name in enumerate(spec.instance_types):
        path = f"runtime.ecs.ec2.instance_types[{index}]"
        if ec2.is_arm(name) != arm:
            yield _error(
                "T17",
                path,
                f"{name} does not match architecture {d.runtime.ecs.architecture}",
                "Graviton types (m7g, c7g, r7g, t4g) for arm64, others for x86_64",
            )
    instance = ec2.spec(spec.instance_types[0])
    if instance is None:
        yield _info(
            "T18",
            "runtime.ecs.ec2.instance_types",
            f"{spec.instance_types[0]} is not in the size table: task fit and "
            "network interfaces are not checked",
        )
        return
    groups = ec2.placement(d)
    for tasks in groups.values():
        for x in tasks:
            if x.cpu > instance.cpu_units or x.memory > instance.task_memory_mib:
                yield _error(
                    "T18",
                    x.path,
                    f"a task of {x.cpu} CPU units and {x.memory} MiB does not fit "
                    f"on {instance.name} ({instance.cpu_units} units, about "
                    f"{instance.task_memory_mib} MiB for tasks)",
                    "pick a larger instance type, or smaller tasks",
                )
    limits = {"on-demand": ("max", spec.max), "spot": ("spot_max", spec.spot_max)}
    for group, tasks in groups.items():
        if not tasks:
            continue
        key, ceiling = limits[group]
        needed = ec2.instances_for(tasks, instance, high=True)
        if needed > ceiling:
            slots = instance.task_slots
            yield _warn(
                "T18",
                f"runtime.ecs.ec2.{key}",
                f"at full scale the {group} tasks need {needed} {instance.name} "
                f"instances, above {key} {ceiling}; each holds {slots} awsvpc "
                f"task(s) at most ({instance.enis} network interfaces)",
                f"raise {key}, pick larger types, or enable ENI trunking: "
                "aws ecs put-account-setting-default --name awsvpcTrunking "
                "--value enabled",
            )


@rule
def storage_bucket(d: Deployment) -> Iterator[Finding]:
    uses_s3 = (
        d.binary_mode == "s3"
        or d.storage.execution_data == "s3"
        or d.storage.exchange.enabled
    )
    if uses_s3 and d.storage.bucket.kind == "none":
        yield _error(
            "T12",
            "storage.bucket",
            "S3 data or an exchange prefix needs a bucket",
            "use {kind: create} or {kind: existing, name: ...}",
        )


@rule
def eks_egress(d: Deployment) -> Iterator[Finding]:
    vpc = d.network.vpc
    if d.runtime.platform == "eks" and vpc.kind == "create" and vpc.nat == "none":
        yield _error(
            "T13",
            "network.vpc.nat",
            "EKS nodes run in private subnets and need NAT for images and APIs",
            "use nat: instance or single",
        )


@rule
def version_pinning(d: Deployment) -> Iterator[Finding]:
    version = lic.parse_version(d.n8n.version)
    if version is None:
        yield _warn(
            "T14",
            "n8n.version",
            f"'{d.n8n.version}' is a moving tag",
            "pin a release such as 2.37.1; the runners image uses the same tag",
        )
    elif version < lic.MIN_VERSION_SUPPORTED:
        yield _error(
            "T14",
            "n8n.version",
            "n8n-on-aws generates configuration for n8n 2.x and later",
        )


@rule
def worker_scaling(d: Deployment) -> Iterator[Finding]:
    workers = d.topology.workers
    if not d.is_queued:
        return
    if workers.min == 0 and workers.scale_on == "cpu":
        yield _error(
            "T16",
            "topology.workers.min",
            "CPU scaling cannot start a worker when none runs",
            "set topology.workers.scale_on: queue-depth, or min: 1",
        )
    if workers.scale_on == "cpu" and not 10 <= workers.target <= 95:
        yield _error(
            "T16",
            "topology.workers.target",
            "with scale_on: cpu, target is a CPU percentage between 10 and 95",
        )


@rule
def operational_advice(d: Deployment) -> Iterator[Finding]:
    if d.is_queued and d.topology.workers.concurrency < 5:
        yield _warn(
            "T15",
            "topology.workers.concurrency",
            "n8n recommends a worker concurrency of 5 or more",
        )
    if d.runtime.ecs.capacity == "fargate-spot" and d.environment == "production":
        yield _warn(
            "T15",
            "runtime.ecs.capacity",
            "main on Fargate Spot can be interrupted, pausing triggers and the editor",
            "keep fargate for main and set topology.workers.spot instead",
        )
    database = d.database
    if (
        database.kind in ("aurora-serverless", "aurora", "rds")
        and d.environment == "production"
        and not database.deletion_protection
    ):
        yield _warn(
            "T15", "database.deletion_protection", "production database is unprotected"
        )
    if d.n8n.telemetry:
        yield _info(
            "T15",
            "n8n.telemetry",
            "n8n sends usage telemetry about this instance to n8n",
            "set n8n.telemetry: false to keep the instance private",
        )
    if d.n8n.runners.mode == "internal":
        yield _warn(
            "T15",
            "n8n.runners.mode",
            "internal task runners are deprecated and insecure",
            "use external (n8nio/runners sidecar)",
        )


# ------------------------------------------------------------------- licence


def _rank(edition: str) -> int:
    return lic.EDITION_RANK[edition]


@rule
def community_usage(d: Deployment) -> Iterator[Finding]:
    license = d.n8n.license
    if license.edition in lic.COMMUNITY_EDITIONS:
        if license.usage is None:
            yield _error(
                "L1",
                "n8n.license.usage",
                f"the {license.edition} edition requires a usage declaration",
                "add usage: {purpose: internal-business, "
                "accept_sustainable_use_license: true}",
            )
            return
        if not license.usage.accept_sustainable_use_license:
            yield _error(
                "L1",
                "n8n.license.usage.accept_sustainable_use_license",
                "the Sustainable Use License must be accepted for community editions",
                f"read {lic.LICENSE_URL}",
            )
        if license.usage.purpose not in lic.COMMUNITY_PURPOSES:
            yield _error(
                "L2",
                "n8n.license.usage.purpose",
                f"'{license.usage.purpose}' is outside the Sustainable Use License, "
                "which allows internal business, non-commercial or personal use only",
                "use a paid edition and agree the terms with n8n",
            )
        return
    usage = license.usage
    if usage is None:
        return
    if usage.purpose == "embedded-editor":
        yield _warn(
            "L3",
            "n8n.license.usage.purpose",
            "showing the n8n editor inside your product needs an OEM agreement "
            "with n8n",
            "contact license@n8n.io",
        )
    if usage.purpose == "service-for-third-parties":
        yield _warn(
            "L4",
            "n8n.license.usage.purpose",
            "running n8n as a service for third parties: check the terms with n8n",
        )
    if (
        license.edition == "business"
        and usage.employees is not None
        and usage.employees >= lic.BUSINESS_MAX_EMPLOYEES
    ):
        yield _warn(
            "L5",
            "n8n.license.usage.employees",
            "n8n positions the Business plan for companies with fewer than "
            f"{lic.BUSINESS_MAX_EMPLOYEES} employees",
            f"consider Enterprise, see {lic.PRICING_URL}",
        )


@rule
def license_key(d: Deployment) -> Iterator[Finding]:
    license = d.n8n.license
    has_key = license.activation_key is not None
    has_cert = license.certificate is not None
    if has_key and has_cert:
        yield _error(
            "L6",
            "n8n.license",
            "set activation_key or certificate, never both",
        )
    if license.edition != "community" and not (has_key or has_cert):
        yield _error(
            "L6",
            "n8n.license.activation_key",
            f"the {license.edition} edition needs a licence activation key",
            "store it in Secrets Manager and reference it: {secret: n8n/license}",
        )
    if license.edition == "registered-community" and has_cert:
        yield _error(
            "L6",
            "n8n.license.certificate",
            "offline certificates are for paid editions",
        )


def _gate(d: Deployment, feature: str, path: str) -> Iterator[Finding]:
    edition = d.n8n.license.edition
    if not lic.allows(edition, feature):
        spec = lic.FEATURES[feature]
        yield _error(
            "L7",
            path,
            f"{spec.name} needs the {spec.edition} edition (configured: {edition})",
            "set n8n.license.edition, or remove the feature",
        )


@rule
def feature_gates(d: Deployment) -> Iterator[Finding]:
    if d.topology.mode == "multi-main":
        yield from _gate(d, "multi-main", "topology.mode")
    if d.binary_mode == "s3":
        yield from _gate(d, "s3-binary-data", "storage.binary_data")
    if d.storage.execution_data == "s3":
        yield from _gate(d, "s3-execution-data", "storage.execution_data")
    if d.auth.n8n_sso.kind != "none":
        yield from _gate(d, "sso", "auth.n8n_sso")


@rule
def raw_env_gates(d: Deployment) -> Iterator[Finding]:
    for variable in sorted(set(d.n8n.env) & lic.RESERVED_LICENSE_ENV):
        yield _error(
            "L8",
            f"n8n.env.{variable}",
            "licence variables are generated from n8n.license",
            "use n8n.license.activation_key or certificate",
        )
    for variable, feature in lic.features_from_env(d.n8n.env):
        yield from _gate(d, feature, f"n8n.env.{variable}")


@rule
def license_server_egress(d: Deployment) -> Iterator[Finding]:
    license = d.n8n.license
    if license.activation_key is None:
        return
    if d.network.vpc.kind == "existing":
        yield _info(
            "L9",
            "network.vpc",
            "the licence key is checked daily against n8n's licence server",
            "make sure the private subnets have internet egress",
        )


@rule
def version_features(d: Deployment) -> Iterator[Finding]:
    version = lic.parse_version(d.n8n.version)
    if version is None:
        return
    if d.auth.n8n_sso.kind != "none" and version < lic.MIN_VERSION_ENV_SSO:
        yield _error(
            "L10",
            "auth.n8n_sso",
            "SSO configured by environment needs n8n 2.18.0 or later",
        )


# -------------------------------------------------------------------- finops


@rule
def finops_schedule(d: Deployment) -> Iterator[Finding]:
    if d.finops.schedule.kind == "office-hours" and d.environment == "production":
        yield _error(
            "F1",
            "finops.schedule.kind",
            "office-hours schedules stop n8n at night; not for production",
            "set environment dev or staging, or use always-on",
        )
    if d.finops.schedule.kind == "office-hours" and d.topology.mode == "multi-main":
        yield _warn(
            "F1",
            "finops.schedule.kind",
            "a scheduled multi-main deployment pays for HA it switches off",
        )


@rule
def finops_estimate_guard(d: Deployment) -> Iterator[Finding]:
    if d.finops.max_monthly_usd is not None:
        yield _info(
            "F2",
            "finops.max_monthly_usd",
            "plan and deploy stop when the monthly estimate is above this ceiling",
        )


@rule
def finops_cost_cliffs(d: Deployment) -> Iterator[Finding]:
    production = d.environment == "production"
    vpc = d.network.vpc
    if vpc.kind == "create":
        if vpc.nat == "per-az" and not production:
            yield _warn(
                "F3",
                "network.vpc.nat",
                f"one NAT gateway per AZ costs ~${35 * vpc.azs}/month before traffic",
                "use nat: single or instance outside production",
            )
        interface = [e for e in vpc.endpoints if e != "s3"]
        if len(interface) >= 3:
            yield _info(
                "F3",
                "network.vpc.endpoints",
                f"{len(interface)} interface endpoints cost "
                f"~${8 * len(interface) * vpc.azs}/month",
            )
    if d.database.kind == "aurora" and not production:
        yield _warn(
            "F3",
            "database.kind",
            "provisioned Aurora has a fixed floor of several hundred USD a month",
            "use aurora-serverless or rds outside production",
        )
    if (
        d.database.kind == "aurora-serverless"
        and d.database.min_acu == 0
        and d.finops.schedule.kind == "always-on"
    ):
        yield _info(
            "F3",
            "database.min_acu",
            "Aurora never pauses while n8n runs: n8n keeps a connection open and "
            "pings the database every 2 seconds",
            "min_acu: 0 saves money only when finops.schedule stops the services",
        )
    if d.queue.kind == "elasticache" and not production:
        yield _warn(
            "F3",
            "queue.kind",
            "node-based ElastiCache bills every node around the clock",
            "use elasticache-serverless outside production",
        )
    if d.ingress.kind == "cloudfront" and d.ingress.cloudfront.price_class == "all":
        yield _info(
            "F3",
            "ingress.cloudfront.price_class",
            "price class 'all' uses the most expensive edge locations",
        )
    if d.observability.log_retention_days == 0:
        yield _warn(
            "F3",
            "observability.log_retention_days",
            "logs kept forever grow cost forever",
        )
    if (
        d.storage.bucket.kind == "create"
        and d.storage.bucket.binary_expiry_days is None
    ):
        yield _warn(
            "F3",
            "storage.bucket.binary_expiry_days",
            "binary data is never expired from S3",
        )
    if d.finops.budget.monthly_usd is None and d.profile in ("nano", "small"):
        yield _info(
            "F4",
            "finops.budget.monthly_usd",
            "no budget alert is set",
            "add finops.budget.monthly_usd and alert_emails",
        )
    if d.finops.budget.monthly_usd is not None and not d.finops.budget.alert_emails:
        yield _warn(
            "F4",
            "finops.budget.alert_emails",
            "a budget without alert emails notifies nobody",
        )


@rule
def finops_metrics(d: Deployment) -> Iterator[Finding]:
    count = (4 if d.is_queued else 0) + (
        5 if d.observability.prometheus == "cloudwatch" else 0
    )
    if d.collects_metrics and count:
        yield _info(
            "F6",
            "observability.prometheus"
            if d.observability.prometheus == "cloudwatch"
            else "topology.workers.scale_on",
            f"a CloudWatch agent publishes {count} n8n metrics: about "
            f"${0.30 * count:.2f}/month, plus a few MB of metric logs",
        )


@rule
def finops_edge_auth(d: Deployment) -> Iterator[Finding]:
    if d.auth.mode == "cloudfront-cognito":
        yield _info(
            "F7",
            "auth.mode",
            "the Lambda@Edge login wall runs on editor requests only: about $0.60 "
            "per million requests plus duration; Cognito is free up to 10,000 "
            "monthly active users",
        )


@rule
def finops_waf(d: Deployment) -> Iterator[Finding]:
    created = []
    if d.ingress.kind == "cloudfront" and (
        d.ingress.cloudfront.waf.kind == "managed" or d.auth.mode == "ip-allowlist"
    ):
        created.append("ingress.cloudfront.waf")
    if d.has_alb and d.ingress.waf.kind == "managed":
        created.append("ingress.waf")
    for path in created:
        yield _info(
            "F8",
            path,
            "a web ACL costs $5/month plus $1/month per rule (up to 5 here) and "
            "$0.60 per million requests",
        )


@rule
def finops_dashboard(d: Deployment) -> Iterator[Finding]:
    if d.observability.dashboard.enabled:
        yield _info(
            "F5",
            "observability.dashboard.enabled",
            "the dashboard is free for the first 3 in the account, then $3/month; "
            "each alarm costs $0.10/month",
        )
    elif d.environment == "production":
        yield _warn(
            "F5",
            "observability.dashboard.enabled",
            "a production deployment without a dashboard is hard to operate",
            "set observability.dashboard.enabled: true",
        )


def evaluate(deployment: Deployment) -> list[Finding]:
    """Run every rule and return the findings, errors first."""
    findings = [finding for check in RULES for finding in check(deployment)]
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(findings, key=lambda f: (order[f.level], f.code, f.path))
