"""The ``<name>-edge`` stack in us-east-1: CloudFront certificate, cost anomalies."""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_ce as ce
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_route53 as route53
from constructs import Construct

from ..config.models import Deployment

EDGE_REGION = "us-east-1"


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


def needs_anomaly_monitor(d: Deployment) -> bool:
    """True when Cost Anomaly Detection is requested and can notify someone."""
    return d.finops.anomaly_detection and bool(d.finops.budget.alert_emails)


class EdgeStack(cdk.Stack):
    """Global resources that AWS only accepts in us-east-1."""

    certificate: acm.ICertificate | None

    def __init__(
        self, scope: Construct, construct_id: str, d: Deployment, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.certificate = None
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
