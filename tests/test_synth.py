"""Assertions on synthesized CloudFormation templates (no AWS call)."""

from __future__ import annotations

import io
import json
from functools import cache
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from n8n_on_aws import cli
from n8n_on_aws.cdk import build_app, synthesis_findings
from n8n_on_aws.config import load

from .conftest import FIXTURES, check

DOMAIN = {
    "domain": "n8n.example.com",
    "hosted_zone": {"kind": "existing", "id": "Z0123456789ABC", "name": "example.com"},
    "certificate": {"kind": "create"},
}


def _app(deployment: Any) -> cdk.App:
    app, findings = build_app(deployment)
    assert app is not None, findings
    return app


@cache
def sample(name: str) -> dict[str, Template]:
    result = load(FIXTURES / f"{name}.yaml")
    assert result.deployment is not None
    return templates(_app(result.deployment))


def templates(app: cdk.App) -> dict[str, Template]:
    found: dict[str, Template] = {}
    for child in app.node.children:
        if isinstance(child, cdk.Stack):
            suffix = child.stack_name.rsplit("-", 1)[-1]
            found[suffix] = Template.from_stack(child)
    return found


def synth(extra: dict[str, Any]) -> dict[str, Template]:
    result = check(extra)
    assert result.ok and result.deployment is not None, result.findings
    return templates(_app(result.deployment))


def containers(template: Template) -> dict[str, list[dict[str, Any]]]:
    found = template.find_resources("AWS::ECS::TaskDefinition")
    return {
        res["Properties"]["Family"]: res["Properties"]["ContainerDefinitions"]
        for res in found.values()
    }


# ------------------------------------------------------------- minimal


def test_minimal_is_cloudfront_to_task_without_alb_or_nat() -> None:
    t = sample("minimal")
    t["network"].resource_count_is("AWS::EC2::NatGateway", 0)
    t["n8n"].resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 0)
    t["n8n"].has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "Origins": [
                        Match.object_like(
                            {
                                "Id": "n8n-task",
                                "CustomOriginConfig": Match.object_like(
                                    {
                                        "HTTPPort": 5678,
                                        "OriginProtocolPolicy": "http-only",
                                    }
                                ),
                            }
                        )
                    ]
                }
            )
        },
    )
    t["n8n"].has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": Match.object_like({"detail-type": ["ECS Task State Change"]})},
    )
    t["n8n"].has_resource_properties(
        "AWS::ECS::Service",
        {
            "NetworkConfiguration": {
                "AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "ENABLED"})
            },
            "DeploymentConfiguration": Match.object_like(
                {"MinimumHealthyPercent": 0, "MaximumPercent": 100}
            ),
        },
    )


def test_minimal_task_has_runner_sidecar_and_ca_init() -> None:
    defs = containers(sample("minimal")["n8n"])
    names = [c["Name"] for c in defs["n8n-minimal-main"]]
    assert names == ["n8n", "rds-ca", "runners"]
    main = defs["n8n-minimal-main"][0]
    assert main["Image"] == "docker.n8n.io/n8nio/n8n:stable"
    runner = defs["n8n-minimal-main"][2]
    assert runner["Image"] == "docker.n8n.io/n8nio/runners:stable"
    secret_names = {s["Name"] for s in main["Secrets"]}
    assert {"N8N_ENCRYPTION_KEY", "DB_POSTGRESDB_PASSWORD"} <= secret_names


def test_encryption_key_is_retained() -> None:
    data = sample("minimal")["data"]
    secrets = data.find_resources(
        "AWS::SecretsManager::Secret",
        {"Properties": {"Name": "n8n-minimal/n8n-encryption-key"}},
    )
    assert [r["DeletionPolicy"] for r in secrets.values()] == ["Retain"]


def test_no_secret_value_in_environment() -> None:
    for name in ("minimal", "queue-workers", "enterprise-ha", "existing-landing-zone"):
        for family, defs in containers(sample(name)["n8n"]).items():
            for container in defs:
                variables = {e["Name"] for e in container.get("Environment", [])}
                leaked = variables & {
                    "N8N_ENCRYPTION_KEY",
                    "N8N_LICENSE_ACTIVATION_KEY",
                    "DB_POSTGRESDB_PASSWORD",
                    "N8N_RUNNERS_AUTH_TOKEN",
                    "N8N_EXTERNAL_STORAGE_S3_ACCESS_SECRET",
                }
                assert not leaked, (name, family, leaked)


def test_every_stack_is_tagged() -> None:
    t = sample("minimal")
    for template in t.values():
        clusters = template.find_resources("AWS::EC2::SecurityGroup")
        for resource in clusters.values():
            tags = {tag["Key"]: tag["Value"] for tag in resource["Properties"]["Tags"]}
            assert tags["n8n-on-aws:deployment"] == "n8n-minimal"
            assert tags["n8n-on-aws:edition"] == "community"


# --------------------------------------------------------- queue workers


def test_queue_workers_alb_oidc_with_open_webhook_paths() -> None:
    t = sample("queue-workers")["n8n"]
    t.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "Port": 443,
            "DefaultActions": Match.array_with(
                [Match.object_like({"Type": "authenticate-oidc"})]
            ),
        },
    )
    rules = t.find_resources("AWS::ElasticLoadBalancingV2::ListenerRule")
    paths = [
        value
        for rule in rules.values()
        for condition in rule["Properties"]["Conditions"]
        for value in condition["PathPatternConfig"]["Values"]
    ]
    assert "/webhook/*" in paths and "/mcp/*" in paths
    assert all(
        rule["Properties"]["Actions"][0]["Type"] == "forward" for rule in rules.values()
    )


def test_queue_workers_on_spot_with_on_demand_base() -> None:
    t = sample("queue-workers")["n8n"]
    t.has_resource_properties(
        "AWS::ECS::Service",
        {
            "ServiceName": "n8n-queue-worker",
            "CapacityProviderStrategy": [
                {"CapacityProvider": "FARGATE", "Base": 1, "Weight": 0},
                {"CapacityProvider": "FARGATE_SPOT", "Weight": 1},
            ],
        },
    )


def test_queue_workers_serverless_valkey_and_budget() -> None:
    t = sample("queue-workers")
    t["data"].has_resource_properties(
        "AWS::ElastiCache::ServerlessCache", {"Engine": "valkey"}
    )
    t["n8n"].has_resource_properties(
        "AWS::Budgets::Budget",
        {"Budget": Match.object_like({"BudgetLimit": {"Amount": 300, "Unit": "USD"}})},
    )
    t["edge"].resource_count_is("AWS::CE::AnomalyMonitor", 1)


def test_bucket_lifecycle_spares_execution_data() -> None:
    t = sample("queue-workers")["data"]
    t.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": Match.array_with(
                    [
                        Match.object_like(
                            {"Prefix": "workflows/", "ExpirationInDays": 30}
                        )
                    ]
                )
            }
        },
    )
    enterprise = sample("enterprise-ha")["data"]
    bucket = next(iter(enterprise.find_resources("AWS::S3::Bucket").values()))
    prefixes = [
        rule.get("Prefix")
        for rule in bucket["Properties"]["LifecycleConfiguration"]["Rules"]
    ]
    assert "workflows/" not in prefixes


# --------------------------------------------------------- enterprise HA


def test_enterprise_multi_main_is_sticky_and_split() -> None:
    t = sample("enterprise-ha")["n8n"]
    t.resource_count_is("AWS::ECS::Service", 3)
    t.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "TargetGroupAttributes": Match.array_with(
                [{"Key": "stickiness.enabled", "Value": "true"}]
            )
        },
    )
    t.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Conditions": [
                {
                    "Field": "path-pattern",
                    "PathPatternConfig": {
                        "Values": [
                            "/webhook/*",
                            "/webhook-waiting/*",
                            "/form/*",
                            "/form-waiting/*",
                        ]
                    },
                }
            ]
        },
    )
    main = containers(t)["n8n-ha-main"][0]
    variables = {e["Name"]: e["Value"] for e in main["Environment"]}
    assert variables["N8N_MULTI_MAIN_SETUP_ENABLED"] == "true"
    assert variables["N8N_DISABLE_PRODUCTION_MAIN_PROCESS"] == "true"


def test_enterprise_data_is_encrypted_with_created_key() -> None:
    t = sample("enterprise-ha")["data"]
    t.resource_count_is("AWS::KMS::Key", 1)
    t.has_resource_properties(
        "AWS::RDS::DBCluster",
        {"StorageEncrypted": True, "KmsKeyId": Match.any_value()},
    )
    t.has_resource_properties(
        "AWS::ElastiCache::ReplicationGroup",
        {"TransitEncryptionEnabled": True, "NumCacheClusters": 3},
    )


def test_permissions_boundary_applied() -> None:
    t = sample("enterprise-ha")["n8n"]
    roles = t.find_resources("AWS::IAM::Role")
    assert roles
    for role in roles.values():
        assert "PermissionsBoundary" in role["Properties"]


# ------------------------------------------------------ landing zone


def test_landing_zone_reuses_vpc_db_and_cognito() -> None:
    t = sample("existing-landing-zone")
    t["network"].resource_count_is("AWS::EC2::VPC", 0)
    t["data"].resource_count_is("AWS::RDS::DBCluster", 0)
    t["data"].has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"GroupId": "sg-0123456789abcdef0", "FromPort": 5432},
    )
    t["n8n"].resource_count_is("AWS::Cognito::UserPool", 0)
    t["n8n"].resource_count_is("AWS::ECS::Cluster", 0)
    t["n8n"].has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internal"}
    )
    t["n8n"].has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
            "DefaultActions": Match.array_with(
                [
                    Match.object_like(
                        {
                            "Type": "authenticate-cognito",
                            "AuthenticateCognitoConfig": Match.object_like(
                                {"UserPoolClientId": "1example23456789"}
                            ),
                        }
                    )
                ]
            )
        },
    )


# -------------------------------------------------------- other shapes


def test_cloudfront_to_alb_in_other_region_uses_edge_certificate() -> None:
    t = synth(
        {
            "aws": {"region": "eu-west-1", "account": "123456789012"},
            "ingress": {
                "kind": "cloudfront",
                "cloudfront": {"origin": "alb"},
                **DOMAIN,
            },
        }
    )
    t["edge"].resource_count_is("AWS::CertificateManager::Certificate", 1)
    t["n8n"].has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internal"}
    )
    t["n8n"].has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {"Aliases": ["n8n.example.com"], "PriceClass": "PriceClass_100"}
            )
        },
    )


def test_created_cognito_with_invitations_and_mfa() -> None:
    t = synth(
        {
            "ingress": {"kind": "alb", **DOMAIN},
            "auth": {
                "mode": "alb-cognito",
                "cognito": {"kind": "create", "mfa": "required", "invite": ["a@b.co"]},
            },
        }
    )["n8n"]
    t.has_resource_properties("AWS::Cognito::UserPool", {"MfaConfiguration": "ON"})
    t.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": True,
            "CallbackURLs": ["https://n8n.example.com/oauth2/idpresponse"],
        },
    )
    t.resource_count_is("AWS::Cognito::UserPoolUser", 1)


def test_office_hours_schedule_scales_to_zero() -> None:
    t = synth({"environment": "dev", "finops": {"schedule": {"kind": "office-hours"}}})[
        "n8n"
    ]
    target = next(
        iter(t.find_resources("AWS::ApplicationAutoScaling::ScalableTarget").values())
    )
    actions = {
        action["ScheduledActionName"]: action["ScalableTargetAction"]
        for action in target["Properties"]["ScheduledActions"]
    }
    stops = [v for k, v in actions.items() if "Stop" in k]
    assert stops == [{"MinCapacity": 0, "MaxCapacity": 0}]


def test_sqlite_on_efs() -> None:
    t = synth({"profile": "nano", "environment": "dev"})
    t["data"].resource_count_is("AWS::EFS::FileSystem", 1)
    defs = containers(t["n8n"])["n8n-test-main"]
    assert defs[0]["MountPoints"] == [
        {
            "ContainerPath": "/home/node/.n8n",
            "ReadOnly": False,
            "SourceVolume": "n8n-home",
        }
    ]
    t["data"].resource_count_is("AWS::RDS::DBCluster", 0)


def test_nat_instance() -> None:
    t = synth({"network": {"vpc": {"kind": "create", "nat": "instance"}}})["network"]
    t.resource_count_is("AWS::EC2::NatGateway", 0)
    t.has_resource_properties("AWS::EC2::Instance", {"InstanceType": "t4g.nano"})


@pytest.mark.parametrize(
    "extra, path",
    [
        (
            {"runtime": {"platform": "eks"}, "ingress": {"kind": "alb", **DOMAIN}},
            "runtime.platform",
        ),
        ({"runtime": {"ecs": {"capacity": "ec2"}}}, "runtime.ecs.capacity"),
        ({"auth": {"mode": "cloudfront-cognito"}}, "auth.mode"),
    ],
)
def test_deferred_features_are_reported(extra: dict[str, Any], path: str) -> None:
    result = check(extra)
    assert result.deployment is not None
    findings = synthesis_findings(result.deployment)
    assert [f.path for f in findings if f.code == "C1"] == [path]
    app, _ = build_app(result.deployment)
    assert app is None


# ------------------------------------------------------------------ cli


def test_cli_dry_run_writes_templates(tmp_path: Any) -> None:
    out = io.StringIO()
    code = cli.main(
        [
            "deploy",
            "-f",
            str(FIXTURES / "minimal.yaml"),
            "--dry-run",
            "-o",
            str(tmp_path),
        ],
        out,
    )
    assert code == 0, out.getvalue()
    assert "n8n-minimal-n8n" in out.getvalue()
    template = json.loads((tmp_path / "n8n-minimal-n8n.template.json").read_text())
    assert "Resources" in template


def test_cli_dry_run_refuses_deferred_features() -> None:
    out = io.StringIO()
    code = cli.main(["deploy", "-f", str(FIXTURES / "nano.yaml"), "--dry-run"], out)
    assert code == 1
    assert "not synthesized yet" in out.getvalue()


def test_cli_deploy_runs_cdk(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/npx")
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    out = io.StringIO()
    path = str(FIXTURES / "minimal.yaml")
    assert cli.main(["deploy", "-f", path, "--set", "n8n.timezone=UTC"], out) == 0
    command = calls[0]
    assert command[:5] == ["npx", "--yes", "aws-cdk@2", "deploy", "--all"]
    assert "n8n_on_aws.cdk" in command[command.index("--app") + 1]
    assert "n8n.timezone=UTC" in command[command.index("--app") + 1]
    assert command[-2:] == ["--require-approval", "broadening"]
    assert cli.main(["destroy", "-f", path, "--yes"], out) == 0
    assert calls[1][3] == "destroy" and calls[1][-1] == "--force"


def test_log_retention_follows_configuration() -> None:
    t = sample("minimal")["n8n"]
    t.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})
