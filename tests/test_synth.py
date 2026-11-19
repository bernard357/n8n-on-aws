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
    assert runner["Image"] == "n8nio/runners:stable"
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
    actions = {
        rule["Properties"]["Priority"]: rule["Properties"]["Actions"][0]["Type"]
        for rule in rules.values()
    }
    # Priority 1 hides n8n's /metrics; every other rule forwards without a login.
    assert actions.pop(1) == "fixed-response"
    assert set(actions.values()) == {"forward"}


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
    path = str(FIXTURES / "eks-new-cluster.yaml")
    code = cli.main(["deploy", "-f", path, "--dry-run"], out)
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


def test_bootstrap_regions_include_edge_when_needed() -> None:
    from n8n_on_aws.cdk import bootstrap_regions

    plain = check({"aws": {"region": "eu-west-3"}})
    assert plain.deployment is not None
    assert bootstrap_regions(plain.deployment) == ["eu-west-3"]
    edge = check(
        {
            "aws": {"region": "eu-west-3"},
            "ingress": {
                "kind": "cloudfront",
                "cloudfront": {"origin": "alb"},
                **DOMAIN,
            },
        }
    )
    assert edge.deployment is not None
    assert bootstrap_regions(edge.deployment) == ["eu-west-3", "us-east-1"]


def test_container_insights_log_group_is_owned_by_the_stack() -> None:
    t = synth({"observability": {"container_insights": True}})["n8n"]
    t.has_resource(
        "AWS::Logs::LogGroup",
        {
            "Properties": {
                "LogGroupName": "/aws/ecs/containerinsights/n8n-test/performance",
                "RetentionInDays": 30,
            },
            "DeletionPolicy": "Delete",
        },
    )


def test_prefix_list_lookup_is_deleted_before_its_log_group() -> None:
    t = sample("minimal")["network"]
    lookups = t.find_resources("Custom::AWS")
    assert lookups
    for resource in lookups.values():
        assert any("CloudFrontPrefixListLogs" in dep for dep in resource["DependsOn"])


# ------------------------------------------------------ queue-depth scaling (#5)


def _policies(template: Template) -> dict[str, dict[str, Any]]:
    found = template.find_resources("AWS::ApplicationAutoScaling::ScalingPolicy")
    return {key: res["Properties"] for key, res in found.items()}


def test_queue_workers_scale_on_waiting_jobs_with_a_cpu_safety_net() -> None:
    t = sample("queue-workers")["n8n"]
    worker = {k: p for k, p in _policies(t).items() if k.startswith("Worker")}
    steps = {
        k: p["StepScalingPolicyConfiguration"]["StepAdjustments"]
        for k, p in worker.items()
        if p["PolicyType"] == "StepScaling"
    }
    waiting = next(v for k, v in steps.items() if "QueueWaiting" in k)
    assert [s["ScalingAdjustment"] for s in waiting] == [1, 2, 4]
    idle = next(v for k, v in steps.items() if "QueueIdle" in k)
    assert [s["ScalingAdjustment"] for s in idle] == [-1]
    tracking = [
        p for p in worker.values() if p["PolicyType"] == "TargetTrackingScaling"
    ]
    assert len(tracking) == 1
    assert tracking[0]["TargetTrackingScalingPolicyConfiguration"]["DisableScaleIn"]
    alarms = t.find_resources("AWS::CloudWatch::Alarm")
    expressions = [
        query["Expression"]
        for alarm in alarms.values()
        for query in alarm["Properties"].get("Metrics", [])
        if "Expression" in query
    ]
    assert "w + a - (FILL(r, 0) - 1) * 10" in expressions


def test_queue_workers_main_runs_a_metrics_agent() -> None:
    t = sample("queue-workers")["n8n"]
    main = next(c for family, c in containers(t).items() if family.endswith("-main"))
    agent = next(c for c in main if c["Name"] == "metrics")
    assert agent["Essential"] is False
    env = {e["Name"]: e["Value"] for e in agent["Environment"]}
    config = json.loads(env["CW_CONFIG_CONTENT"])
    emf = config["logs"]["metrics_collected"]["prometheus"]["emf_processor"]
    selectors = emf["metric_declaration"][0]["metric_selectors"]
    assert "^n8n_scaling_mode_queue_jobs_waiting$" in selectors
    assert emf["metric_namespace"] == "n8n-on-aws"
    n8n = next(c for c in main if c["Name"] == "n8n")
    n8n_env = {e["Name"]: e["Value"] for e in n8n["Environment"]}
    assert n8n_env["N8N_METRICS"] == "true"
    assert n8n_env["N8N_METRICS_INCLUDE_QUEUE_METRICS"] == "true"
    t.has_resource_properties(
        "AWS::Logs::LogGroup", {"LogGroupName": "/n8n-on-aws/n8n-queue/metrics"}
    )


def test_metrics_path_is_hidden_on_the_alb() -> None:
    t = sample("queue-workers")["n8n"]
    t.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 1,
            "Conditions": [
                Match.object_like(
                    {"PathPatternConfig": {"Values": ["/metrics", "/metrics/*"]}}
                )
            ],
            "Actions": [Match.object_like({"Type": "fixed-response"})],
        },
    )


def test_cpu_scaling_has_no_agent_and_tracks_cpu() -> None:
    t = synth(
        {
            "topology": {
                "mode": "queue",
                "workers": {"scale_on": "cpu", "target": 65},
            },
            "queue": {"kind": "elasticache-serverless"},
        }
    )["n8n"]
    families = containers(t)
    assert all(c["Name"] != "metrics" for cs in families.values() for c in cs)
    worker = [p for k, p in _policies(t).items() if k.startswith("Worker")]
    assert [p["PolicyType"] for p in worker] == ["TargetTrackingScaling"]
    config = worker[0]["TargetTrackingScalingPolicyConfiguration"]
    assert config["TargetValue"] == 65
    assert t.find_resources("AWS::CloudFront::Function") == {}


def test_prometheus_on_cloudfront_hides_metrics_at_the_edge() -> None:
    t = synth({"observability": {"prometheus": "cloudwatch"}})["n8n"]
    t.resource_count_is("AWS::CloudFront::Function", 1)
    t.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "CacheBehaviors": [
                        Match.object_like(
                            {
                                "PathPattern": "/metrics*",
                                "FunctionAssociations": Match.any_value(),
                            }
                        )
                    ]
                }
            )
        },
    )
    main = next(iter(containers(t).values()))
    agent = next(c for c in main if c["Name"] == "metrics")
    env = {e["Name"]: e["Value"] for e in agent["Environment"]}
    selectors = json.loads(env["CW_CONFIG_CONTENT"])["logs"]["metrics_collected"][
        "prometheus"
    ]["emf_processor"]["metric_declaration"][0]["metric_selectors"]
    assert "^n8n_active_workflow_count$" in selectors
    assert not any("queue" in s for s in selectors)


def test_queue_depth_scaling_is_no_longer_degraded() -> None:
    result = load(FIXTURES / "queue-workers.yaml")
    assert result.deployment is not None
    paths = {f.path for f in synthesis_findings(result.deployment)}
    assert "topology.workers.scale_on" not in paths
    assert "observability.prometheus" not in paths


# ------------------------------------------------- Cognito at the edge (#6)

EDGE_WALL = {
    "aws": {"region": "eu-west-3"},
    "ingress": {"kind": "cloudfront", **DOMAIN},
    "auth": {"mode": "cloudfront-cognito"},
}


def test_edge_wall_function_lives_in_us_east_1_and_is_retained() -> None:
    stacks = synth(EDGE_WALL)
    edge = stacks["edge"]
    functions = edge.find_resources("AWS::Lambda::Function")
    wall = next(f for f in functions.values() if f["Properties"].get("FunctionName"))
    assert wall["Properties"]["FunctionName"] == "n8n-test-edge-auth"
    assert wall["Properties"]["Runtime"] == "nodejs22.x"
    assert wall["Properties"]["MemorySize"] == 128
    assert wall["Properties"]["Timeout"] == 5
    assert wall["DeletionPolicy"] == "Retain"
    assert "Environment" not in wall["Properties"]
    roles = json.dumps(edge.find_resources("AWS::IAM::Role"))
    assert "edgelambda.amazonaws.com" in roles


def test_edge_wall_guards_the_editor_and_leaves_public_paths_open() -> None:
    t = synth(EDGE_WALL)["n8n"]
    config = next(iter(t.find_resources("AWS::CloudFront::Distribution").values()))[
        "Properties"
    ]["DistributionConfig"]
    default = config["DefaultCacheBehavior"]
    associations = default["LambdaFunctionAssociations"]
    assert [a["EventType"] for a in associations] == ["viewer-request"]
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    for path in ("/webhook/*", "/form/*", "/mcp/*", "/healthz*", "/mcp-server/*"):
        assert path in behaviors, path
        assert "LambdaFunctionAssociations" not in behaviors[path], path
    assert "/webhook-test/*" not in behaviors
    assert "/rest/*" not in behaviors


def test_edge_wall_uses_a_public_cognito_client_and_writes_its_config() -> None:
    t = synth(EDGE_WALL)["n8n"]
    t.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "CallbackURLs": ["https://n8n.example.com/_auth/callback"],
            "LogoutURLs": ["https://n8n.example.com/"],
            "AllowedOAuthFlows": ["code"],
        },
    )
    writers = t.find_resources("Custom::AWS")
    create = json.dumps(
        [w["Properties"].get("Create") for w in writers.values()], default=str
    )
    assert "putParameter" in create and "/n8n-on-aws/n8n-test/edge-auth" in create
    assert "us-east-1" in create


def test_edge_wall_in_us_east_1_keeps_the_function_in_the_n8n_stack() -> None:
    stacks = synth({**EDGE_WALL, "aws": {"region": "us-east-1"}})
    assert "edge" not in stacks
    stacks["n8n"].has_resource_properties(
        "AWS::Lambda::Function", {"FunctionName": "n8n-test-edge-auth"}
    )


def test_edge_wall_with_existing_cognito_reports_the_client_needs() -> None:
    result = check(
        {
            **EDGE_WALL,
            "auth": {
                "mode": "cloudfront-cognito",
                "cognito": {
                    "kind": "existing",
                    "user_pool_id": "eu-west-3_AbCdEf",
                    "client_id": "abc123",
                    "domain": "my-login",
                },
            },
        }
    )
    assert result.ok and result.deployment is not None, result.findings
    messages = [f.message for f in synthesis_findings(result.deployment)]
    assert any("/_auth/callback" in m for m in messages)
    t = templates(_app(result.deployment))["n8n"]
    t.resource_count_is("AWS::Cognito::UserPool", 0)
    writers = json.dumps(
        [w["Properties"] for w in t.find_resources("Custom::AWS").values()],
        default=str,
    )
    assert "my-login.auth." in writers


def _fake_aws(stderr_on_delete: str = "") -> tuple[list[list[str]], Any]:
    calls: list[list[str]] = []

    def run(command: list[str], **_: Any) -> Any:
        calls.append(command)
        stdout = ""
        if "describe-regions" in command:
            stdout = "eu-west-3 us-east-1 eu-central-1"
        elif "describe-log-groups" in command:
            prefix = command[command.index("--log-group-name-prefix") + 1]
            stdout = f"{prefix}network-CustomVpcRestrictDefaultSG-abc"
        if "delete-function" in command and stderr_on_delete:
            return cli.subprocess.CompletedProcess(command, 254, "", stderr_on_delete)
        return cli.subprocess.CompletedProcess(command, 0, stdout, "")

    return calls, run


def test_cli_cleanup_deletes_the_edge_function_and_its_log_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, run = _fake_aws()
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(cli.subprocess, "run", run)
    out = io.StringIO()
    path = str(FIXTURES / "small-team.yaml")
    assert cli.main(["cleanup", "-f", path, "--yes"], out) == 0
    function = next(c for c in calls if "delete-function" in c)
    assert "n8n-team-edge-auth" in function
    groups = [
        c[c.index("--log-group-name") + 1] for c in calls if "delete-log-group" in c
    ]
    assert groups.count("/aws/lambda/us-east-1.n8n-team-edge-auth") == 3
    assert "/aws/lambda/n8n-team-network-CustomVpcRestrictDefaultSG-abc" in groups


def test_cli_cleanup_waits_for_replicas(monkeypatch: pytest.MonkeyPatch) -> None:
    _, run = _fake_aws("Lambda was unable to delete ... replicated function")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(cli.subprocess, "run", run)
    out = io.StringIO()
    path = str(FIXTURES / "small-team.yaml")
    assert cli.main(["cleanup", "-f", path, "--yes"], out) == 1
    assert "run cleanup again later" in out.getvalue()


def test_cli_cleanup_without_edge_wall_only_deletes_helper_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, run = _fake_aws()
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(cli.subprocess, "run", run)
    out = io.StringIO()
    assert (
        cli.main(["cleanup", "-f", str(FIXTURES / "minimal.yaml"), "--yes"], out) == 0
    )
    assert not any("delete-function" in c for c in calls)
    assert "deleted log group /aws/lambda/n8n-minimal-network-" in out.getvalue()


def test_edge_wall_parameter_is_owned_by_the_edge_stack() -> None:
    stacks = synth(EDGE_WALL)
    stacks["edge"].has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": "/n8n-on-aws/n8n-test/edge-auth", "Value": "{}"},
    )
    writers = stacks["n8n"].find_resources("Custom::AWS")
    assert writers
    assert all("Delete" not in w["Properties"] for w in writers.values())
    own = synth({**EDGE_WALL, "aws": {"region": "us-east-1"}})["n8n"]
    own.resource_count_is("Custom::AWS", 0)
    own.has_resource_properties(
        "AWS::SSM::Parameter", {"Name": "/n8n-on-aws/n8n-test/edge-auth"}
    )


# ---------------------------------------------- WAF and allow-list on CloudFront (#7)

EDGE_WAF = {
    "aws": {"region": "eu-west-3"},
    "ingress": {
        "kind": "cloudfront",
        **DOMAIN,
        "cloudfront": {"waf": {"kind": "managed", "rate_limit": 500}},
    },
}


def _rules(template: Template) -> dict[str, dict[str, Any]]:
    acl = next(iter(template.find_resources("AWS::WAFv2::WebACL").values()))
    return {r["Name"]: r for r in acl["Properties"]["Rules"]}


def test_cloudfront_web_acl_lives_in_us_east_1_and_is_attached() -> None:
    stacks = synth(EDGE_WAF)
    stacks["edge"].has_resource_properties(
        "AWS::WAFv2::WebACL", {"Scope": "CLOUDFRONT", "Name": "n8n-test-edge"}
    )
    assert list(_rules(stacks["edge"])) == [
        "RateLimit",
        "AWSManagedRulesCommonRuleSet",
        "AWSManagedRulesKnownBadInputsRuleSet",
    ]
    distribution = next(
        iter(stacks["n8n"].find_resources("AWS::CloudFront::Distribution").values())
    )
    assert "WebACLId" in distribution["Properties"]["DistributionConfig"]
    stacks["n8n"].resource_count_is("AWS::WAFv2::WebACL", 0)


def test_allow_list_blocks_editor_paths_and_keeps_public_paths_open() -> None:
    stacks = synth(
        {
            "aws": {"region": "eu-west-3"},
            "ingress": {
                "kind": "cloudfront",
                **DOMAIN,
                "allowed_cidrs": ["198.51.100.0/24", "2001:db8::/32"],
            },
            "auth": {"mode": "ip-allowlist", "public_paths": ["/hooks/*"]},
        }
    )
    edge = stacks["edge"]
    sets = edge.find_resources("AWS::WAFv2::IPSet")
    versions = {s["Properties"]["IPAddressVersion"] for s in sets.values()}
    assert versions == {"IPV4", "IPV6"}
    rules = _rules(edge)
    assert list(rules) == ["AllowList"]
    statement = rules["AllowList"]["Statement"]["AndStatement"]["Statements"]
    addresses, public = statement
    assert "OrStatement" in addresses["NotStatement"]["Statement"]
    paths = {
        s["ByteMatchStatement"]["SearchString"]: s["ByteMatchStatement"][
            "PositionalConstraint"
        ]
        for s in public["NotStatement"]["Statement"]["OrStatement"]["Statements"]
    }
    assert paths["/webhook/"] == "STARTS_WITH"
    assert paths["/healthz"] == "STARTS_WITH"
    assert paths["/rest/binary-data/signed"] == "EXACTLY"
    assert paths["/hooks/"] == "STARTS_WITH"
    assert "/rest/" not in paths and "/webhook-test/" not in paths


def test_web_acl_hides_metrics_whatever_the_case() -> None:
    stacks = synth({**EDGE_WAF, "observability": {"prometheus": "cloudwatch"}})
    rule = _rules(stacks["edge"])["HideMetrics"]
    match = rule["Statement"]["ByteMatchStatement"]
    assert match["SearchString"] == "/metrics"
    assert match["TextTransformations"][0]["Type"] == "LOWERCASE"
    assert "Block" in rule["Action"]


def test_cloudfront_web_acl_in_us_east_1_stays_in_the_n8n_stack() -> None:
    stacks = synth({**EDGE_WAF, "aws": {"region": "us-east-1"}})
    assert "edge" not in stacks
    stacks["n8n"].has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "CLOUDFRONT"})


def test_alb_web_acl_uses_the_same_rules() -> None:
    t = sample("queue-workers")["n8n"]
    if not t.find_resources("AWS::WAFv2::WebACL"):
        t = synth(
            {
                "ingress": {"kind": "alb", **DOMAIN, "waf": {"kind": "managed"}},
            }
        )["n8n"]
    t.has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "REGIONAL"})
    assert "RateLimit" in _rules(t)
