"""ECS on EC2 capacity (#10): rules, synthesis, dashboard, cost and assistant."""

from __future__ import annotations

import io
from functools import cache
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from n8n_on_aws import cli
from n8n_on_aws.cdk import build_app, synthesis_findings
from n8n_on_aws.cdk.dashboard import DashboardBuilder, unmonitored
from n8n_on_aws.config import instances, load
from n8n_on_aws.finops.estimate import compute_floor, estimate, price_table
from n8n_on_aws.plan import build_plan

from .conftest import FIXTURES, check

EC2 = {
    "runtime": {"ecs": {"capacity": "ec2"}},
    "network": {"vpc": {"kind": "create", "nat": "single"}},
    "ingress": {"kind": "alb"},
}
QUEUED = {
    "topology": {
        "mode": "queue",
        "workers": {"min": 1, "max": 6, "spot": True},
    },
    "queue": {"kind": "elasticache-serverless"},
}


def _codes(extra: dict[str, Any], code: str) -> list[tuple[str, str]]:
    result = check(extra)
    return [(f.level, f.path) for f in result.findings if f.code == code]


@cache
def _sample() -> tuple[cdk.App, Template]:
    result = load(FIXTURES / "ec2-capacity.yaml")
    assert result.ok and result.deployment is not None, result.findings
    app, findings = build_app(result.deployment)
    assert app is not None, findings
    stack = app.node.find_child("n8n-ec2-n8n")
    assert isinstance(stack, cdk.Stack)
    return app, Template.from_stack(stack)


# --------------------------------------------------------------- sizes


def test_instance_table() -> None:
    m7g = instances.spec("m7g.large")
    assert m7g is not None
    assert (m7g.vcpu, m7g.memory_gib, m7g.task_slots, m7g.arm) == (2, 8, 2, True)
    r7i = instances.spec("r7i.2xlarge")
    assert r7i is not None and (r7i.vcpu, r7i.memory_gib, r7i.arm) == (8, 64, False)
    t4g = instances.spec("t4g.small")
    assert t4g is not None and (t4g.memory_gib, t4g.enis) == (2, 3)
    assert instances.is_arm("c7gn.large") and not instances.is_arm("m7i-flex.large")
    assert instances.spec("x2gd.large") is None


# --------------------------------------------------------------- rules


def test_ec2_is_no_longer_deferred() -> None:
    result = check(EC2)
    assert result.ok and result.deployment is not None, result.findings
    assert not [f for f in synthesis_findings(result.deployment) if f.code == "C1"]


def test_cloudfront_to_the_task_is_rejected() -> None:
    extra = {**EC2, "ingress": {"kind": "cloudfront", "cloudfront": {"origin": "task"}}}
    assert ("error", "ingress.cloudfront.origin") in _codes(extra, "T17")


def test_tasks_on_ec2_need_a_nat() -> None:
    extra = {**EC2, "network": {"vpc": {"kind": "create", "nat": "none"}}}
    assert ("error", "network.vpc.nat") in _codes(extra, "T17")


def test_an_existing_cluster_is_rejected() -> None:
    extra = {
        **EC2,
        "runtime": {
            "ecs": {"capacity": "ec2", "cluster": {"kind": "existing", "name": "x"}}
        },
    }
    assert ("error", "runtime.ecs.cluster") in _codes(extra, "T17")


def test_instance_types_must_match_the_architecture() -> None:
    extra = {
        **EC2,
        "runtime": {
            "ecs": {
                "capacity": "ec2",
                "ec2": {"instance_types": ["m7g.large", "m7i.large"]},
            }
        },
    }
    assert _codes(extra, "T17") == [("error", "runtime.ecs.ec2.instance_types[1]")]


def test_a_task_larger_than_the_instance_is_rejected() -> None:
    extra = {
        **EC2,
        "runtime": {
            "ecs": {"capacity": "ec2", "ec2": {"instance_types": ["t4g.small"]}}
        },
        "topology": {"main": {"cpu": 2048, "memory": 4096}},
    }
    assert ("error", "topology.main") in _codes(extra, "T18")


def test_too_few_network_interfaces_at_full_scale_warn() -> None:
    extra = {
        **EC2,
        **QUEUED,
        "runtime": {"ecs": {"capacity": "ec2", "ec2": {"spot_max": 2}}},
    }
    # 5 Spot workers on m7g.large (2 awsvpc tasks each) need 3 instances.
    assert _codes(extra, "T18") == [("warning", "runtime.ecs.ec2.spot_max")]
    raised = {**extra, "runtime": {"ecs": {"capacity": "ec2", "ec2": {"spot_max": 3}}}}
    assert _codes(raised, "T18") == []


def test_an_unknown_instance_type_skips_the_size_checks() -> None:
    extra = {
        **EC2,
        "runtime": {
            "ecs": {"capacity": "ec2", "ec2": {"instance_types": ["x2gd.large"]}}
        },
    }
    assert _codes(extra, "T18") == [("info", "runtime.ecs.ec2.instance_types")]


def test_placement_keeps_the_first_worker_on_demand() -> None:
    result = check({**EC2, **QUEUED})
    assert result.deployment is not None
    groups = instances.placement(result.deployment)
    assert [(x.path, x.low, x.high) for x in groups["on-demand"]] == [
        ("topology.main", 1, 1),
        ("topology.workers", 1, 1),
    ]
    assert [(x.path, x.low, x.high) for x in groups["spot"]] == [
        ("topology.workers", 0, 5)
    ]


# ----------------------------------------------------------- synthesis


def test_sample_hosts_are_graviton_with_imdsv2_and_encrypted_disks() -> None:
    _, t = _sample()
    t.resource_count_is("AWS::EC2::LaunchTemplate", 2)
    t.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {
            "LaunchTemplateName": "n8n-ec2-on-demand",
            "LaunchTemplateData": Match.object_like(
                {
                    "InstanceType": "m7g.large",
                    "ImageId": {
                        "Ref": Match.string_like_regexp(
                            "amazonlinux2023arm64recommended"
                        )
                    },
                    "MetadataOptions": {
                        "HttpTokens": "required",
                        "HttpPutResponseHopLimit": 1,
                    },
                    "BlockDeviceMappings": [
                        Match.object_like(
                            {"Ebs": Match.object_like({"Encrypted": True})}
                        )
                    ],
                }
            ),
        },
    )
    spot = t.find_resources(
        "AWS::EC2::LaunchTemplate",
        {"Properties": {"LaunchTemplateName": "n8n-ec2-spot"}},
    )
    user_data = str(next(iter(spot.values()))["Properties"]["LaunchTemplateData"])
    assert "ECS_ENABLE_SPOT_INSTANCE_DRAINING=true" in user_data


def test_sample_groups_and_capacity_providers() -> None:
    _, t = _sample()
    t.has_resource_properties(
        "AWS::AutoScaling::AutoScalingGroup",
        {
            "AutoScalingGroupName": "n8n-ec2-on-demand",
            "MinSize": "1",
            "MaxSize": "3",
            "MixedInstancesPolicy": Match.object_like(
                {
                    "InstancesDistribution": Match.object_like(
                        {"OnDemandPercentageAboveBaseCapacity": 100}
                    )
                }
            ),
        },
    )
    t.has_resource_properties(
        "AWS::AutoScaling::AutoScalingGroup",
        {
            "AutoScalingGroupName": "n8n-ec2-spot",
            "MinSize": "0",
            "MaxSize": "4",
            "MixedInstancesPolicy": Match.object_like(
                {
                    "InstancesDistribution": Match.object_like(
                        {
                            "OnDemandPercentageAboveBaseCapacity": 0,
                            "SpotAllocationStrategy": "price-capacity-optimized",
                        }
                    )
                }
            ),
        },
    )
    providers = t.find_resources("AWS::ECS::CapacityProvider")
    assert len(providers) == 2
    for provider in providers.values():
        group = provider["Properties"]["AutoScalingGroupProvider"]
        # Neither blocks destroy: no protected instance, tasks drained first.
        assert group["ManagedTerminationProtection"] == "DISABLED"
        assert group["ManagedDraining"] == "ENABLED"
        assert group["ManagedScaling"]["Status"] == "ENABLED"


def test_sample_services_run_on_the_providers_and_go_first_on_delete() -> None:
    _, t = _sample()
    tasks = t.find_resources("AWS::ECS::TaskDefinition")
    for task in tasks.values():
        assert task["Properties"]["RequiresCompatibilities"] == ["EC2"]
        assert task["Properties"]["NetworkMode"] == "awsvpc"
        assert "RuntimePlatform" not in task["Properties"]
    associations = t.find_resources("AWS::ECS::ClusterCapacityProviderAssociations")
    association = next(iter(associations))
    services = t.find_resources("AWS::ECS::Service")
    strategies = {}
    for service in services.values():
        props = service["Properties"]
        assert association in service["DependsOn"]
        assert "LaunchType" not in props
        assert (
            props["NetworkConfiguration"]["AwsvpcConfiguration"].get(
                "AssignPublicIp", "DISABLED"
            )
            == "DISABLED"
        )
        strategies[props["ServiceName"]] = [
            (s["CapacityProvider"]["Ref"][:8], s.get("Base", 0), s["Weight"])
            for s in props["CapacityProviderStrategy"]
        ]
    assert strategies == {
        "n8n-ec2-main": [("HostsCap", 0, 1)],
        "n8n-ec2-worker": [("HostsCap", 1, 0), ("SpotHost", 0, 1)],
    }


def test_queue_workers_sample_with_capacity_set_to_ec2() -> None:
    result = load(
        FIXTURES / "queue-workers.yaml",
        ["runtime.ecs.capacity=ec2"],
    )
    assert result.ok and result.deployment is not None, result.findings
    app, findings = build_app(result.deployment)
    assert app is not None, findings
    stack = app.node.find_child("n8n-queue-n8n")
    assert isinstance(stack, cdk.Stack)
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::AutoScaling::AutoScalingGroup", 2)
    t.resource_count_is("AWS::ECS::CapacityProvider", 2)


def test_without_spot_workers_there_is_one_group() -> None:
    result = check(EC2)
    assert result.deployment is not None
    app, _ = build_app(result.deployment)
    assert app is not None
    stack = app.node.find_child("n8n-test-n8n")
    assert isinstance(stack, cdk.Stack)
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::AutoScaling::AutoScalingGroup", 1)
    t.has_resource_properties(
        "AWS::ECS::Service",
        {"CapacityProviderStrategy": [Match.object_like({"Weight": 1})]},
    )


def test_x86_hosts_use_the_standard_image() -> None:
    extra = {
        **EC2,
        "runtime": {
            "ecs": {
                "capacity": "ec2",
                "architecture": "x86_64",
                "ec2": {"instance_types": ["m7i.large"]},
            }
        },
    }
    result = check(extra)
    assert result.ok and result.deployment is not None, result.findings
    app, _ = build_app(result.deployment)
    assert app is not None
    stack = app.node.find_child("n8n-test-n8n")
    assert isinstance(stack, cdk.Stack)
    Template.from_stack(stack).has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {
            "LaunchTemplateData": Match.object_like(
                {
                    "ImageId": {
                        "Ref": Match.string_like_regexp("amazonlinux2023recommended")
                    }
                }
            )
        },
    )


# ----------------------------------------------------------- dashboard


def test_dashboard_has_a_hosts_section_covering_the_groups() -> None:
    app, _ = _sample()
    stack = app.node.find_child("n8n-ec2-n8n")
    builder = getattr(stack, "operations", None)
    assert isinstance(builder, DashboardBuilder)
    assert "ECS hosts (EC2 capacity)" in builder.titles
    assert unmonitored(app, builder) == []


# ---------------------------------------------------------------- cost


def test_cost_counts_hosts_not_fargate_tasks() -> None:
    result = load(FIXTURES / "ec2-capacity.yaml")
    assert result.deployment is not None
    cost = estimate(result.deployment, build_plan(result.deployment))
    compute = [i for i in cost.items if i.component == "compute"]
    assert [i.item.split(":")[0] for i in compute] == [
        "EC2 on-demand hosts",
        "EC2 spot hosts",
    ]
    prices = price_table().regions[cost.priced_in]
    on_demand = compute[0]
    disk = prices["ebs_gp3_gb_month"] * 30
    assert on_demand.fixed == pytest.approx(prices["ec2_m7g.large_hour"] * 730 + disk)
    assert compute[1].fixed == 0  # the Spot group starts from zero


def test_f9_compares_hosts_with_fargate() -> None:
    result = load(FIXTURES / "ec2-capacity.yaml")
    assert result.deployment is not None
    hosts, fargate = compute_floor(result.deployment, build_plan(result.deployment))
    assert hosts > 0 and fargate > 0
    out = io.StringIO()
    code = cli.main(["plan", "-f", str(FIXTURES / "ec2-capacity.yaml")], out=out)
    assert code == 0
    assert "F9" in out.getvalue() and "Fargate" in out.getvalue()
