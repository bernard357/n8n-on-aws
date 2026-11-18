"""From a validated deployment to the n8n roles, their settings and the stacks.

This module is pure: no AWS call, no CDK import. Values only known at deployment
time (endpoints, bucket names) are written as ``${placeholder}`` and resolved by the
CDK stacks.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config.licensing import parse_version
from .config.models import Deployment, SecretRef
from .config.rules import Finding

RoleName = Literal["main", "webhook", "worker"]

# Paths that external callers reach without the login wall (see 03-deployment-options).
PUBLIC_PATHS = [
    "/webhook/*",
    "/webhook-waiting/*",
    "/form/*",
    "/form-waiting/*",
    "/mcp/*",
    "/healthz*",
]


class Role(BaseModel):
    """One n8n process type and how it runs."""

    name: RoleName
    command: str
    replicas_min: int
    replicas_max: int
    cpu: int
    memory: int
    scaling: str
    routes: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, SecretRef] = Field(default_factory=dict)


class Plan(BaseModel):
    """The resolved deployment, ready to be rendered or synthesized."""

    name: str
    profile: str
    environment: str
    edition: str
    purpose: str | None
    url: str
    platform: str
    stacks: list[str]
    roles: list[Role]
    public_paths: list[str]
    data_services: dict[str, str]
    tags: dict[str, str]
    findings: list[Finding] = Field(default_factory=list)


def _url(d: Deployment) -> tuple[str, str, str]:
    """Return (host, protocol, base URL)."""
    if d.ingress.domain:
        host = d.ingress.domain
        protocol = "https"
    elif d.ingress.kind == "cloudfront":
        host = "${cloudfront.domain}"
        protocol = "https"
    else:
        host = "${alb.dns_name}"
        protocol = "http"
    return host, protocol, f"{protocol}://{host}/"


def _proxy_hops(d: Deployment) -> int:
    if d.ingress.kind == "cloudfront":
        return 2 if d.ingress.cloudfront.origin == "alb" else 1
    return 1


def _common_env(d: Deployment) -> tuple[dict[str, str], dict[str, SecretRef]]:
    host, protocol, url = _url(d)
    n8n = d.n8n
    env: dict[str, str] = {
        "N8N_HOST": host,
        "N8N_PORT": "5678",
        "N8N_PROTOCOL": protocol,
        "N8N_EDITOR_BASE_URL": url,
        "WEBHOOK_URL": url,
        "N8N_PROXY_HOPS": str(_proxy_hops(d)),
        "GENERIC_TIMEZONE": n8n.timezone,
        "TZ": n8n.timezone,
        "N8N_LOG_LEVEL": d.observability.log_level,
        "N8N_LOG_OUTPUT": "console",
        "N8N_RUNNERS_MODE": n8n.runners.mode,
        "N8N_RUNNERS_MAX_CONCURRENCY": str(n8n.runners.max_concurrency),
        "EXECUTIONS_TIMEOUT": str(n8n.executions.timeout_seconds),
        "EXECUTIONS_DATA_PRUNE": "true",
        "EXECUTIONS_DATA_MAX_AGE": str(n8n.executions.max_age_hours),
        "EXECUTIONS_DATA_PRUNE_MAX_COUNT": str(n8n.executions.max_count),
        "EXECUTIONS_DATA_SAVE_ON_SUCCESS": n8n.executions.save_on_success,
        "N8N_PUBLIC_API_DISABLED": "false" if n8n.public_api else "true",
        "N8N_METRICS": "true" if n8n.metrics else "false",
    }
    version = parse_version(n8n.version)
    if version is None or version >= (2, 30, 0):
        env["N8N_WEBHOOK_URL"] = url
    secrets: dict[str, SecretRef] = {
        "N8N_ENCRYPTION_KEY": SecretRef(
            secret=n8n.encryption_key.secret
            if n8n.encryption_key.kind == "existing"
            else f"{d.name}/n8n-encryption-key"
        ),
    }
    if n8n.runners.mode == "external":
        secrets["N8N_RUNNERS_AUTH_TOKEN"] = SecretRef(
            secret=f"{d.name}/n8n-runners-token"
        )
    if n8n.license.activation_key:
        secrets["N8N_LICENSE_ACTIVATION_KEY"] = n8n.license.activation_key
    if n8n.license.certificate:
        secrets["N8N_LICENSE_CERT"] = n8n.license.certificate

    database = d.database
    if database.kind == "sqlite":
        env["DB_TYPE"] = "sqlite"  # under /home/node/.n8n, on EFS
    else:
        env["DB_TYPE"] = "postgresdb"
        env["DB_POSTGRESDB_POOL_SIZE"] = str(database.pool_size)
        if database.kind == "external":
            env["DB_POSTGRESDB_HOST"] = database.host
            env["DB_POSTGRESDB_PORT"] = str(database.port)
            env["DB_POSTGRESDB_DATABASE"] = database.name
            env["DB_POSTGRESDB_SCHEMA"] = database.db_schema
            credentials = database.credentials
            ssl = database.ssl
        else:
            env["DB_POSTGRESDB_HOST"] = "${database.endpoint}"
            env["DB_POSTGRESDB_PORT"] = "5432"
            env["DB_POSTGRESDB_DATABASE"] = "n8n"
            credentials = SecretRef(secret="${database.secret}")
            ssl = "verify-full"
        secrets["DB_POSTGRESDB_USER"] = credentials.model_copy(
            update={"key": "username"}
        )
        secrets["DB_POSTGRESDB_PASSWORD"] = credentials.model_copy(
            update={"key": "password"}
        )
        if ssl != "disable":
            env["DB_POSTGRESDB_SSL_ENABLED"] = "true"
            env["DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED"] = (
                "true" if ssl == "verify-full" else "false"
            )
            if ssl == "verify-full":
                env["DB_POSTGRESDB_SSL_CA_FILE"] = "/etc/ssl/rds/global-bundle.pem"

    queue = d.queue
    if d.is_queued and queue.kind != "none":
        env["EXECUTIONS_MODE"] = "queue"
        env["N8N_REDIS_KEY_PREFIX"] = queue.key_prefix
        env["OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS"] = (
            "true" if n8n.executions.offload_manual_to_workers else "false"
        )
        if queue.kind == "external":
            env["QUEUE_BULL_REDIS_HOST"] = queue.host
            env["QUEUE_BULL_REDIS_PORT"] = str(queue.port)
            env["QUEUE_BULL_REDIS_TLS"] = "true" if queue.tls else "false"
            if queue.cluster:
                env["QUEUE_BULL_REDIS_CLUSTER_NODES"] = f"{queue.host}:{queue.port}"
                env["QUEUE_BULL_PREFIX"] = "{" + queue.key_prefix + "}"
            if queue.auth:
                secrets["QUEUE_BULL_REDIS_PASSWORD"] = queue.auth
        else:
            env["QUEUE_BULL_REDIS_HOST"] = "${queue.endpoint}"
            env["QUEUE_BULL_REDIS_PORT"] = "6379"
            env["QUEUE_BULL_REDIS_TLS"] = "true"
            if queue.kind == "elasticache-serverless":
                env["QUEUE_BULL_REDIS_CLUSTER_NODES"] = "${queue.endpoint}:6379"
                env["QUEUE_BULL_PREFIX"] = "{" + queue.key_prefix + "}"

    binary = d.binary_mode
    env["N8N_DEFAULT_BINARY_DATA_MODE"] = binary
    if binary == "s3" or d.storage.execution_data == "s3":
        bucket = d.storage.bucket
        region = d.aws.region or "${aws.region}"
        env["N8N_AVAILABLE_BINARY_DATA_MODES"] = "filesystem,s3"
        env["N8N_EXTERNAL_STORAGE_S3_HOST"] = f"s3.{region}.amazonaws.com"
        env["N8N_EXTERNAL_STORAGE_S3_BUCKET_NAME"] = (
            bucket.name if bucket.kind == "existing" else "${storage.bucket}"
        )
        env["N8N_EXTERNAL_STORAGE_S3_BUCKET_REGION"] = region
        env["N8N_EXTERNAL_STORAGE_S3_AUTH_AUTO_DETECT"] = "true"
    if d.storage.execution_data == "s3":
        env["N8N_EXECUTION_DATA_STORAGE_MODE"] = "s3"

    sso = d.auth.n8n_sso
    if sso.kind != "none":
        env["N8N_SSO_MANAGED_BY_ENV"] = "true"
        env["N8N_SSO_USER_ROLE_PROVISIONING"] = sso.role_provisioning
        if sso.kind == "oidc":
            env["N8N_SSO_OIDC_LOGIN_ENABLED"] = "true"
            env["N8N_SSO_OIDC_CLIENT_ID"] = sso.client_id
            env["N8N_SSO_OIDC_DISCOVERY_ENDPOINT"] = sso.discovery_endpoint
            secrets["N8N_SSO_OIDC_CLIENT_SECRET"] = sso.client_secret
        else:
            env["N8N_SSO_SAML_LOGIN_ENABLED"] = "true"
            env["N8N_SSO_SAML_METADATA_URL"] = sso.metadata_url

    if d.topology.mode == "multi-main":
        env["N8N_MULTI_MAIN_SETUP_ENABLED"] = "true"
    secrets.update(n8n.secret_env)
    return env, secrets


def _apply_user_env(
    roles: list[Role], user_env: dict[str, str], findings: list[Finding]
) -> None:
    generated = set().union(*(role.env for role in roles))
    for variable in sorted(set(user_env) & generated):
        findings.append(
            Finding(
                level="warning",
                code="P1",
                path=f"n8n.env.{variable}",
                message="overrides a value generated by n8n-on-aws",
            )
        )
    for role in roles:
        role.env.update(user_env)


def build_plan(d: Deployment, findings: list[Finding] | None = None) -> Plan:
    """Turn a validated deployment into roles, stacks and data services."""
    env, secrets = _common_env(d)
    topology = d.topology
    roles: list[Role] = []

    main_env = dict(env)
    split = topology.mode in ("split", "multi-main")
    if split:
        main_env["N8N_DISABLE_PRODUCTION_MAIN_PROCESS"] = "true"
    roles.append(
        Role(
            name="main",
            command="n8n start",
            replicas_min=topology.main.replicas,
            replicas_max=topology.main.replicas,
            cpu=topology.main.cpu,
            memory=topology.main.memory,
            scaling="fixed",
            routes=["/*"] if not split else ["/* (except webhook paths)"],
            env=main_env,
            secrets=dict(secrets),
        )
    )
    if split:
        webhook = topology.webhook
        roles.append(
            Role(
                name="webhook",
                command="n8n webhook",
                replicas_min=webhook.min,
                replicas_max=webhook.max,
                cpu=webhook.cpu,
                memory=webhook.memory,
                scaling=f"cpu {webhook.scale_on_cpu}%",
                routes=[
                    "/webhook/*",
                    "/webhook-waiting/*",
                    "/form/*",
                    "/form-waiting/*",
                ],
                env=dict(env),
                secrets=dict(secrets),
            )
        )
    if d.is_queued:
        workers = topology.workers
        worker_env = dict(env)
        worker_env["QUEUE_HEALTH_CHECK_ACTIVE"] = "true"
        scaling = (
            f"queue depth {workers.target}/worker"
            if workers.scale_on == "queue-depth"
            else f"cpu {workers.target}%"
        )
        roles.append(
            Role(
                name="worker",
                command=f"n8n worker --concurrency={workers.concurrency}",
                replicas_min=workers.min,
                replicas_max=workers.max,
                cpu=workers.cpu,
                memory=workers.memory,
                scaling=scaling + (" (spot)" if workers.spot else ""),
                env=worker_env,
                secrets=dict(secrets),
            )
        )

    all_findings = list(findings or [])
    _apply_user_env(roles, d.n8n.env, all_findings)

    stacks = [f"{d.name}-network", f"{d.name}-data", f"{d.name}-n8n"]
    if d.auth.mode == "cloudfront-cognito":
        stacks.append(f"{d.name}-edge (us-east-1)")

    services: dict[str, str] = {"database": d.database.kind, "queue": d.queue.kind}
    services["binary data"] = d.binary_mode
    services["execution data"] = d.storage.execution_data
    if d.storage.bucket.kind != "none":
        services["bucket"] = d.storage.bucket.kind
    if d.needs_efs:
        services["efs"] = "create"

    tags = {
        "n8n-on-aws:deployment": d.name,
        "n8n-on-aws:environment": d.environment,
        "n8n-on-aws:edition": d.n8n.license.edition,
        **d.aws.tags,
    }
    usage = d.n8n.license.usage
    _, _, url = _url(d)
    public = PUBLIC_PATHS + d.auth.public_paths if d.auth.mode != "none" else []
    return Plan(
        name=d.name,
        profile=d.profile,
        environment=d.environment,
        edition=d.n8n.license.edition,
        purpose=usage.purpose if usage else None,
        url=url,
        platform=f"{d.runtime.platform}/{d.runtime.ecs.capacity}"
        if d.runtime.platform == "ecs"
        else f"eks/{d.runtime.eks.cluster.kind}",
        stacks=stacks,
        roles=roles,
        public_paths=public,
        data_services=services,
        tags=tags,
        findings=all_findings,
    )
