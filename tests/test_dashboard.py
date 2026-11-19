"""The generated CloudWatch dashboard and alarms (no AWS call)."""

from __future__ import annotations

import io
import json
from functools import cache
from typing import Any

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Template

from n8n_on_aws import cli
from n8n_on_aws.cdk import build_app
from n8n_on_aws.cdk.dashboard import DashboardBuilder, unmonitored
from n8n_on_aws.config import resolve

from .conftest import FIXTURES, check

# Every ECS sample, with deferred features (C1) switched off so it synthesizes.
SAMPLES = [
    "minimal",
    "nano",
    "small-team",
    "queue-workers",
    "split-webhooks",
    "enterprise-ha",
    "existing-landing-zone",
]


def _sample_data(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((FIXTURES / f"{name}.yaml").read_text())
    cloudfront = data.get("ingress", {}).get("cloudfront", {})
    if "waf" in cloudfront:
        cloudfront["waf"] = {"kind": "none"}
    observability = data.setdefault("observability", {})
    observability.setdefault("dashboard", {})["enabled"] = True
    return data


def _build(data: dict[str, Any]) -> tuple[cdk.App, DashboardBuilder]:
    result = resolve(data)
    assert result.ok and result.deployment is not None, result.findings
    app, findings = build_app(result.deployment)
    assert app is not None, findings
    service = app.node.find_child(f"{result.deployment.name}-n8n")
    builder = getattr(service, "operations", None)
    assert isinstance(builder, DashboardBuilder)
    return app, builder


@cache
def sample(name: str) -> tuple[cdk.App, DashboardBuilder]:
    return _build(_sample_data(name))


def custom(extra: dict[str, Any]) -> tuple[cdk.App, DashboardBuilder]:
    from .conftest import BASE, merge

    return _build(merge(BASE, extra))


def n8n_template(app: cdk.App, builder: DashboardBuilder) -> Template:
    return Template.from_stack(builder.service)


def body(app: cdk.App, builder: DashboardBuilder) -> str:
    """The dashboard body with tokens flattened to text."""
    template = n8n_template(app, builder)
    dashboards = template.find_resources("AWS::CloudWatch::Dashboard")
    assert len(dashboards) == 1
    value = next(iter(dashboards.values()))["Properties"]["DashboardBody"]
    return json.dumps(value)


# --------------------------------------------------------------- coverage


@pytest.mark.parametrize("name", SAMPLES)
def test_every_sample_gets_a_dashboard_covering_its_resources(name: str) -> None:
    app, builder = sample(name)
    assert builder.dashboard is not None
    assert unmonitored(app, builder) == []
    assert builder.titles[0].startswith("n8n ")
    assert builder.titles[1] == "Health"
    assert builder.titles[-1] == "Cost"


def test_a_resource_without_a_widget_is_reported() -> None:
    app, builder = custom({})
    lambda_.Function(
        builder.service,
        "Stray",
        runtime=lambda_.Runtime.PYTHON_3_13,
        handler="index.handler",
        code=lambda_.Code.from_inline("def handler(e, c): pass"),
    )
    missing = unmonitored(app, builder)
    assert len(missing) == 1 and missing[0].endswith("/Stray/Resource")


# ------------------------------------------------------------------ shapes


def test_cloudfront_task_shape() -> None:
    app, builder = sample("minimal")
    titles = builder.titles
    assert "Ingress: CloudFront" in titles
    assert "Ingress: load balancer" not in titles
    assert "Origin updater" in titles
    assert "Database: Aurora PostgreSQL" in titles
    assert not any(t.startswith(("Queue", "Network", "File system")) for t in titles)
    text = body(app, builder)
    assert '\\"region\\":\\"us-east-1\\"' in text
    assert "AWS/CloudFront" in text


def test_cloudfront_metrics_query_us_east_1_from_another_region() -> None:
    app, builder = custom({"aws": {"region": "eu-west-3"}})
    text = body(app, builder)
    assert "AWS/CloudFront" in text
    widget = text[text.index("AWS/CloudFront") - 200 : text.index("AWS/CloudFront")]
    assert '\\"region\\":\\"us-east-1\\"' in widget
    assert "eu-west-3" in text  # the other widgets stay in the stack's region


def test_queue_shape_with_alb_nat_and_serverless_cache() -> None:
    app, builder = sample("queue-workers")
    titles = builder.titles
    assert "Ingress: load balancer" in titles
    assert "Ingress: CloudFront" not in titles
    assert "Queue: ElastiCache Serverless" in titles
    assert "Network: NAT gateway" in titles
    text = body(app, builder)
    assert "ThrottledCmds" in text and "worker CPU" in text


def test_node_based_cache_and_provisioned_aurora() -> None:
    app, builder = sample("enterprise-ha")
    titles = builder.titles
    assert "Queue: ElastiCache" in titles
    assert "Database: Aurora PostgreSQL" in titles
    text = body(app, builder)
    assert "n8n-ha-queue-001" in text and "n8n-ha-queue-002" in text
    assert "ServerlessDatabaseCapacity" not in text


def test_rds_section() -> None:
    app, builder = custom({"database": {"kind": "rds"}})
    assert "Database: RDS PostgreSQL" in builder.titles
    assert "FreeStorageSpace" in body(app, builder)


def test_sqlite_on_efs_has_files_and_no_database() -> None:
    app, builder = sample("nano")
    titles = builder.titles
    assert "File system: EFS" in titles
    assert not any(t.startswith("Database") for t in titles)


def test_nat_instance_section() -> None:
    app, builder = custom({"network": {"vpc": {"kind": "create", "nat": "instance"}}})
    assert "Network: NAT instance" in builder.titles
    assert unmonitored(app, builder) == []


def test_external_resources_say_they_are_not_monitored() -> None:
    app, builder = sample("existing-landing-zone")
    assert "Network" in builder.titles
    assert "is not monitored here" in body(app, builder)
    app, builder = custom(
        {
            "database": {
                "kind": "external",
                "host": "db.example.com",
                "credentials": {"secret": "n8n/db"},
            }
        }
    )
    assert "Database" in builder.titles
    assert "db.example.com" in body(app, builder)


def test_container_insights_adds_task_counts_and_runner_metrics() -> None:
    app, builder = custom({"observability": {"container_insights": True}})
    text = body(app, builder)
    assert "RunningTaskCount" in text and "ContainerCpuUtilized" in text
    app, builder = custom({})
    assert "need Container Insights" in body(app, builder)


# ------------------------------------------------------------------ alarms


def _alarms(app: cdk.App, builder: DashboardBuilder) -> dict[str, Any]:
    """The operator alarms; scaling alarms belong to Application Auto Scaling."""
    alarms = n8n_template(app, builder).find_resources("AWS::CloudWatch::Alarm")
    return {k: a for k, a in alarms.items() if "AlarmName" in a["Properties"]}


def test_alarms_exist_without_emails_and_notify_nobody() -> None:
    app, builder = sample("minimal")
    alarms = _alarms(app, builder)
    names = {a["Properties"]["AlarmName"] for a in alarms.values()}
    assert {
        "n8n-minimal-main-cpu",
        "n8n-minimal-main-down",
        "n8n-minimal-database-at-max",
        "n8n-minimal-origin-updater-errors",
    } <= names
    assert all("AlarmActions" not in a["Properties"] for a in alarms.values())
    n8n_template(app, builder).resource_count_is("AWS::SNS::Topic", 0)


def test_alarms_notify_emails_and_are_all_on_the_dashboard() -> None:
    app, builder = custom({"observability": {"alarms": {"email": ["ops@example.com"]}}})
    alarms = _alarms(app, builder)
    assert alarms
    assert all("AlarmActions" in a["Properties"] for a in alarms.values())
    text = body(app, builder)
    for logical_id in alarms:
        assert logical_id in text


def test_office_hours_skip_the_main_down_alarm() -> None:
    app, builder = custom(
        {"environment": "dev", "finops": {"schedule": {"kind": "office-hours"}}}
    )
    names = {a["Properties"]["AlarmName"] for a in _alarms(app, builder).values()}
    assert "n8n-test-main-down" not in names


def test_alb_alarms() -> None:
    app, builder = sample("queue-workers")
    names = {a["Properties"]["AlarmName"] for a in _alarms(app, builder).values()}
    assert {"n8n-queue-errors5xx", "n8n-queue-main-unhealthy"} <= names


# ------------------------------------------------------------- settings


def test_dashboard_disabled_keeps_alarms_and_drops_output() -> None:
    app, builder = custom({"observability": {"dashboard": {"enabled": False}}})
    template = n8n_template(app, builder)
    template.resource_count_is("AWS::CloudWatch::Dashboard", 0)
    assert "DashboardUrl" not in template.find_outputs("*")
    assert _alarms(app, builder)


def test_nano_profile_has_no_dashboard_by_default() -> None:
    result = check({"profile": "nano", "environment": "dev"})
    assert result.deployment is not None
    assert not result.deployment.observability.dashboard.enabled


def test_range_period_and_output() -> None:
    app, builder = custom(
        {"observability": {"dashboard": {"range": "7d", "period_minutes": 15}}}
    )
    text = body(app, builder)
    assert '\\"start\\":\\"-P7D\\"' in text
    assert '\\"period\\":900' in text
    outputs = n8n_template(app, builder).find_outputs("DashboardUrl")
    assert outputs


def test_dry_run_lists_dashboard_sections(tmp_path: Any) -> None:
    out = io.StringIO()
    path = str(FIXTURES / "minimal.yaml")
    code = cli.main(["deploy", "-f", path, "--dry-run", "-o", str(tmp_path)], out)
    assert code == 0, out.getvalue()
    assert "dashboard: n8n n8n-minimal, Health, Ingress: CloudFront" in out.getvalue()


def test_plan_shows_operations() -> None:
    out = io.StringIO()
    path = str(FIXTURES / "enterprise-ha.yaml")
    assert cli.main(["plan", "-f", path], out) == 0
    assert "operations  : dashboard n8n-ha; alarms email" in out.getvalue()


def test_error_queries_skip_migration_lines() -> None:
    app, builder = sample("minimal")
    text = body(app, builder)
    assert "not like /migration/" in text


# ------------------------------------------------------------ queue jobs (#5)


def test_queue_jobs_section_and_backlog_alarm() -> None:
    app, builder = sample("queue-workers")
    assert "Queue jobs" in builder.titles
    assert "n8n_scaling_mode_queue_jobs_waiting" in body(app, builder)
    names = {a["Properties"]["AlarmName"] for a in _alarms(app, builder).values()}
    assert "n8n-queue-queue-backlog" in names


def test_process_section_only_with_prometheus() -> None:
    _, plain = sample("minimal")
    assert "n8n process (main)" not in plain.titles
    _, builder = custom({"observability": {"prometheus": "cloudwatch"}})
    assert "n8n process (main)" in builder.titles
    assert "Queue jobs" not in builder.titles
