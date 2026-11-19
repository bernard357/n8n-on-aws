"""What the CDK synthesis covers today, and what is deferred to later issues."""

from __future__ import annotations

from ..config.models import Deployment
from ..config.rules import Finding


def _not_yet(path: str, feature: str) -> Finding:
    return Finding(
        level="error",
        code="C1",
        path=path,
        message=f"{feature} is not synthesized yet",
        hint="see the roadmap in README.md; validate and plan already work",
    )


def _degraded(path: str, message: str) -> Finding:
    return Finding(level="warning", code="C2", path=path, message=message)


def synthesis_findings(d: Deployment) -> list[Finding]:
    """Findings that only matter when the deployment is turned into stacks."""
    findings: list[Finding] = []
    if d.runtime.platform == "eks":
        findings.append(_not_yet("runtime.platform", "EKS"))
    if d.runtime.platform == "ecs" and d.runtime.ecs.capacity == "ec2":
        findings.append(_not_yet("runtime.ecs.capacity", "ECS on EC2 capacity"))
    if d.ingress.kind == "cloudfront" and d.ingress.cloudfront.waf.kind != "none":
        findings.append(_not_yet("ingress.cloudfront.waf", "WAF on CloudFront"))
    if d.ingress.kind == "cloudfront" and d.auth.mode == "ip-allowlist":
        findings.append(_not_yet("auth.mode", "IP allow-list on CloudFront"))
    if d.backup.kind == "aws-backup" and d.backup.cross_region:
        findings.append(
            _degraded("backup.cross_region", "cross-region backup copies are ignored")
        )
    if d.observability.prometheus == "amp":
        findings.append(
            _degraded(
                "observability.prometheus",
                "Amazon Managed Service for Prometheus is not wired yet; ignored",
            )
        )
    if d.finops.anomaly_detection and not d.finops.budget.alert_emails:
        findings.append(
            _degraded(
                "finops.anomaly_detection",
                "anomaly detection needs finops.budget.alert_emails; skipped",
            )
        )
    if d.auth.mode == "cloudfront-cognito" and d.auth.cognito.kind == "existing":
        findings.append(
            Finding(
                level="info",
                code="C3",
                path="auth.cognito.client_id",
                message="the existing app client must have no secret, the code "
                f"grant, and https://{d.ingress.domain}/_auth/callback as a "
                "callback URL",
            )
        )
    if d.auth.mode == "alb-cognito" and d.auth.cognito.kind == "existing":
        findings.append(
            Finding(
                level="info",
                code="C3",
                path="auth.cognito.client_id",
                message="the existing app client must have a secret, the code "
                f"grant, and https://{d.ingress.domain}/oauth2/idpresponse as a "
                "callback URL",
            )
        )
    return findings
