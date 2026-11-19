"""The ``<name>-edge`` stack in us-east-1: CloudFront certificate, cost anomalies."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_ce as ce
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from ..config.models import Deployment
from .waf import needs_edge_waf, web_acl

EDGE_REGION = "us-east-1"
EDGE_AUTH_SOURCE = Path(__file__).parent / "edge_auth_function"
EDGE_AUTH_CALLBACK = "/_auth/callback"
EDGE_AUTH_SIGNOUT = "/_auth/signout"


def hosted_zone(scope: Construct, d: Deployment) -> route53.IHostedZone | None:
    """The Route 53 zone of the deployment's domain, looked up or imported."""
    zone = d.ingress.hosted_zone
    if zone.kind == "lookup":
        return route53.HostedZone.from_lookup(scope, "Zone", domain_name=zone.name)
    if zone.kind == "existing":
        return route53.HostedZone.from_hosted_zone_attributes(
            scope, "Zone", hosted_zone_id=zone.id, zone_name=zone.name
        )
    return None


def needs_edge_certificate(d: Deployment) -> bool:
    """True when CloudFront needs a certificate that we create."""
    return (
        d.ingress.kind == "cloudfront"
        and d.ingress.domain is not None
        and d.ingress.certificate.kind == "create"
    )


def needs_edge_auth(d: Deployment) -> bool:
    """True when a Lambda@Edge function enforces the Cognito login wall."""
    return d.auth.mode == "cloudfront-cognito"


def edge_auth_function_name(d: Deployment) -> str:
    return f"{d.name}-edge-auth"


def edge_auth_parameter(d: Deployment) -> str:
    """SSM parameter in us-east-1 with the user pool and app client of the wall."""
    return f"/n8n-on-aws/{d.name}/edge-auth"


def edge_auth_function(scope: Construct, d: Deployment) -> lambda_.Function:
    """The viewer-request function of the login wall, in a us-east-1 stack.

    Lambda@Edge has no environment variables, so the static settings are written
    next to the code at synth time. The function is retained on delete: AWS removes
    its replicas hours after the distribution lets it go, and only then can it be
    deleted (``n8n-on-aws cleanup``).
    """
    assert d.ingress.domain is not None
    stack = cdk.Stack.of(scope)
    parameter = edge_auth_parameter(d)
    directory = Path(tempfile.mkdtemp(prefix="n8n-edge-auth-"))
    shutil.copy(EDGE_AUTH_SOURCE / "index.mjs", directory / "index.mjs")
    (directory / "config.json").write_text(
        json.dumps(
            {
                "domain": d.ingress.domain,
                "callbackPath": EDGE_AUTH_CALLBACK,
                "signoutPath": EDGE_AUTH_SIGNOUT,
                "sessionSeconds": d.auth.session_hours * 3600,
                "parameter": parameter,
                "parameterRegion": EDGE_REGION,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    role = iam.Role(
        scope,
        "EdgeAuthRole",
        assumed_by=iam.CompositePrincipal(
            iam.ServicePrincipal("lambda.amazonaws.com"),
            iam.ServicePrincipal("edgelambda.amazonaws.com"),
        ),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        ],
        description=f"Lambda@Edge login wall of {d.name}",
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:{stack.partition}:ssm:{EDGE_REGION}:{stack.account}"
                f":parameter{parameter}"
            ],
        )
    )
    function = lambda_.Function(
        scope,
        "EdgeAuth",
        function_name=edge_auth_function_name(d),
        description=f"Cognito login wall of n8n {d.name} at CloudFront",
        runtime=lambda_.Runtime.NODEJS_22_X,
        architecture=lambda_.Architecture.X86_64,
        handler="index.handler",
        code=lambda_.Code.from_asset(str(directory)),
        memory_size=128,
        timeout=cdk.Duration.seconds(5),
        role=role,
        current_version_options=lambda_.VersionOptions(
            removal_policy=cdk.RemovalPolicy.RETAIN
        ),
    )
    function.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
    return function


def needs_anomaly_monitor(d: Deployment) -> bool:
    """True when Cost Anomaly Detection is requested and can notify someone."""
    return d.finops.anomaly_detection and bool(d.finops.budget.alert_emails)


class EdgeStack(cdk.Stack):
    """Global resources that AWS only accepts in us-east-1."""

    certificate: acm.ICertificate | None
    auth_function: lambda_.Function | None
    web_acl: wafv2.CfnWebACL | None

    def __init__(
        self, scope: Construct, construct_id: str, d: Deployment, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.certificate = None
        self.auth_function = edge_auth_function(self, d) if needs_edge_auth(d) else None
        if self.auth_function is not None:
            # Filled in by the n8n stack once the user pool exists; owned here, so it
            # is deleted with this stack and nothing writes to it at delete time.
            ssm.StringParameter(
                self,
                "EdgeAuthConfig",
                parameter_name=edge_auth_parameter(d),
                string_value="{}",
                description=f"user pool and client of the n8n {d.name} login wall",
            )
        self.web_acl = web_acl(self, d, "CLOUDFRONT") if needs_edge_waf(d) else None
        if needs_edge_certificate(d):
            assert d.ingress.domain is not None
            zone = hosted_zone(self, d)
            self.certificate = acm.Certificate(
                self,
                "Certificate",
                domain_name=d.ingress.domain,
                validation=acm.CertificateValidation.from_dns(zone),
            )
        if needs_anomaly_monitor(d):
            monitor = ce.CfnAnomalyMonitor(
                self,
                "CostMonitor",
                monitor_name=f"{d.name}-costs",
                monitor_type="CUSTOM",
                monitor_specification=json.dumps(
                    {"Tags": {"Key": "n8n-on-aws:deployment", "Values": [d.name]}}
                ),
            )
            ce.CfnAnomalySubscription(
                self,
                "CostAlerts",
                subscription_name=f"{d.name}-cost-anomalies",
                frequency="DAILY",
                monitor_arn_list=[monitor.attr_monitor_arn],
                subscribers=[
                    ce.CfnAnomalySubscription.SubscriberProperty(
                        address=email, type="EMAIL"
                    )
                    for email in d.finops.budget.alert_emails
                ],
                threshold_expression=json.dumps(
                    {
                        "Dimensions": {
                            "Key": "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
                            "MatchOptions": ["GREATER_THAN_OR_EQUAL"],
                            "Values": ["10"],
                        }
                    }
                ),
            )
