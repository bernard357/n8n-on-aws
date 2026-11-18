"""Small helpers shared by the stacks."""

from __future__ import annotations

from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from ..config.models import SecretRef

_RETENTION: list[tuple[int, logs.RetentionDays]] = [
    (1, logs.RetentionDays.ONE_DAY),
    (3, logs.RetentionDays.THREE_DAYS),
    (5, logs.RetentionDays.FIVE_DAYS),
    (7, logs.RetentionDays.ONE_WEEK),
    (14, logs.RetentionDays.TWO_WEEKS),
    (30, logs.RetentionDays.ONE_MONTH),
    (60, logs.RetentionDays.TWO_MONTHS),
    (90, logs.RetentionDays.THREE_MONTHS),
    (120, logs.RetentionDays.FOUR_MONTHS),
    (150, logs.RetentionDays.FIVE_MONTHS),
    (180, logs.RetentionDays.SIX_MONTHS),
    (365, logs.RetentionDays.ONE_YEAR),
    (400, logs.RetentionDays.THIRTEEN_MONTHS),
    (545, logs.RetentionDays.EIGHTEEN_MONTHS),
    (731, logs.RetentionDays.TWO_YEARS),
    (1096, logs.RetentionDays.THREE_YEARS),
    (1827, logs.RetentionDays.FIVE_YEARS),
    (2192, logs.RetentionDays.SIX_YEARS),
    (2557, logs.RetentionDays.SEVEN_YEARS),
    (2922, logs.RetentionDays.EIGHT_YEARS),
    (3288, logs.RetentionDays.NINE_YEARS),
    (3653, logs.RetentionDays.TEN_YEARS),
]


def retention(days: int) -> logs.RetentionDays:
    """The smallest CloudWatch retention that keeps logs at least ``days``."""
    if days == 0:
        return logs.RetentionDays.INFINITE
    for allowed, member in _RETENTION:
        if allowed >= days:
            return member
    return logs.RetentionDays.TEN_YEARS


def import_secret(
    scope: Construct, construct_id: str, ref: SecretRef | str
) -> secretsmanager.ISecret:
    """Reference an existing secret by name or complete ARN."""
    name = ref if isinstance(ref, str) else ref.secret
    if name.startswith("arn:"):
        return secretsmanager.Secret.from_secret_complete_arn(scope, construct_id, name)
    return secretsmanager.Secret.from_secret_name_v2(scope, construct_id, name)


def _latest(versions: type, prefix: str) -> object:
    names = [n for n in dir(versions) if n.startswith(prefix)]
    if not names:
        raise ValueError(f"no engine version matches {prefix}")
    names.sort(key=lambda n: [int(p) for p in n.split("_")[1:] if p.isdigit()])
    return getattr(versions, names[-1])


def aurora_version(major: str) -> rds.AuroraPostgresEngineVersion:
    """The newest Aurora PostgreSQL version known to CDK for ``major`` (or x.y)."""
    prefix = "VER_" + major.replace(".", "_")
    version = _latest(rds.AuroraPostgresEngineVersion, prefix)
    assert isinstance(version, rds.AuroraPostgresEngineVersion)
    return version


def postgres_version(major: str) -> rds.PostgresEngineVersion:
    """The newest RDS PostgreSQL version known to CDK for ``major`` (or x.y)."""
    prefix = "VER_" + major.replace(".", "_")
    version = _latest(rds.PostgresEngineVersion, prefix)
    assert isinstance(version, rds.PostgresEngineVersion)
    return version


def chunks(items: list[str], size: int) -> list[list[str]]:
    """Split ``items`` into lists of at most ``size`` elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]
