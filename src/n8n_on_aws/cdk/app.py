"""Assemble the CDK app for one deployment."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from pydantic import BaseModel, Field

from ..config.models import Deployment
from ..config.rules import Finding
from ..plan import build_plan
from .dashboard import DashboardBuilder
from .data import DataStack
from .edge import EDGE_REGION, EdgeStack, needs_anomaly_monitor, needs_edge_certificate
from .network import NetworkStack
from .service import ServiceStack
from .support import synthesis_findings

# Used for in-process synthesis when no account is configured: lookups then return
# dummy values and nothing is sent to AWS. Real deployments get the CLI's account.
PLACEHOLDER_ACCOUNT = "000000000000"
DEFAULT_REGION = "us-east-1"


class StackSummary(BaseModel):
    """One synthesized stack and what it contains."""

    name: str
    region: str
    resources: dict[str, int]
    dashboard: list[str] = Field(default_factory=list)


def environment(d: Deployment) -> cdk.Environment:
    """Account and region: from the file, else from the CDK CLI or AWS settings."""
    account = (
        d.aws.account or os.environ.get("CDK_DEFAULT_ACCOUNT") or PLACEHOLDER_ACCOUNT
    )
    region = (
        d.aws.region
        or os.environ.get("CDK_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )
    return cdk.Environment(account=account, region=region)


def bootstrap_regions(d: Deployment) -> list[str]:
    """Regions where the stacks of ``d`` are deployed, hence need a CDK bootstrap."""
    region = environment(d).region
    assert region is not None
    regions = [region]
    if region != EDGE_REGION and (
        needs_edge_certificate(d) or needs_anomaly_monitor(d)
    ):
        regions.append(EDGE_REGION)
    return regions


def build_app(
    d: Deployment, outdir: str | Path | None = None
) -> tuple[cdk.App | None, list[Finding]]:
    """Build the CDK app, or return None with the blocking findings."""
    findings = synthesis_findings(d)
    if any(f.level == "error" for f in findings):
        return None, findings
    plan = build_plan(d)
    findings.extend(plan.findings)
    app = cdk.App(outdir=str(outdir) if outdir else None)
    env = environment(d)
    assert env.region is not None
    cross_region = env.region != EDGE_REGION
    description = "n8n-on-aws: {} of " + d.name

    edge: EdgeStack | None = None
    if cross_region and (needs_edge_certificate(d) or needs_anomaly_monitor(d)):
        edge = EdgeStack(
            app,
            f"{d.name}-edge",
            d,
            env=cdk.Environment(account=env.account, region=EDGE_REGION),
            cross_region_references=True,
            description=description.format("global resources"),
        )
    network = NetworkStack(
        app, f"{d.name}-network", d, env=env, description=description.format("network")
    )
    data = DataStack(
        app,
        f"{d.name}-data",
        d,
        network,
        env=env,
        description=description.format("data services"),
    )
    service = ServiceStack(
        app,
        f"{d.name}-n8n",
        d,
        plan,
        network,
        data,
        edge,
        env=env,
        cross_region_references=edge is not None,
        description=description.format("n8n services"),
    )
    service.operations = DashboardBuilder(d, plan, network, data, service)
    service.operations.build()
    if not cross_region and needs_anomaly_monitor(d):
        EdgeStack(app, f"{d.name}-edge", d, env=env, description=description)

    for key, value in plan.tags.items():
        cdk.Tags.of(app).add(key, value)
    for stack in app.node.children:
        if isinstance(stack, cdk.Stack):
            component = stack.stack_name.removeprefix(f"{d.name}-")
            cdk.Tags.of(stack).add("n8n-on-aws:component", component)
            if d.aws.permissions_boundary:
                iam.PermissionsBoundary.of(stack).apply(
                    iam.ManagedPolicy.from_managed_policy_arn(
                        stack, "PermissionsBoundary", d.aws.permissions_boundary
                    )
                )
    return app, findings


def synthesize(
    d: Deployment, outdir: str | Path
) -> tuple[list[StackSummary], list[Finding]]:
    """Synthesize into ``outdir`` and summarize the stacks; no AWS call is made."""
    app, findings = build_app(d, outdir)
    if app is None:
        return [], findings
    assembly = app.synth()
    service = app.node.try_find_child(f"{d.name}-n8n")
    builder = getattr(service, "operations", None)
    sections = builder.titles if builder and builder.dashboard else []
    summaries: list[StackSummary] = []
    for artifact in assembly.stacks:
        template = artifact.template
        counts = Counter(
            resource["Type"] for resource in template.get("Resources", {}).values()
        )
        summaries.append(
            StackSummary(
                name=artifact.stack_name,
                region=artifact.environment.region,
                resources=dict(sorted(counts.items())),
                dashboard=sections if artifact.stack_name == f"{d.name}-n8n" else [],
            )
        )
    return summaries, findings
