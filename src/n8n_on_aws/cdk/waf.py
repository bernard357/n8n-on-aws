"""AWS WAF web ACLs: on the ALB (regional) or on CloudFront (global, in us-east-1).

One builder for both scopes, so the managed rules are the same everywhere:

- ``HideMetrics``: n8n serves ``/metrics`` without authentication and its router
  ignores case, so the rule lowercases the path before matching.
- ``AllowList`` (``auth.mode: ip-allowlist``): requests from other addresses are
  blocked, except on the public paths, so external webhook callers still work.
- ``RateLimit`` and two AWS managed rule groups (``ingress...waf.kind: managed``).
"""

from __future__ import annotations

import ipaddress
from typing import Any, Literal

from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from ..config.models import Deployment
from ..plan import PUBLIC_PATHS

Scope = Literal["REGIONAL", "CLOUDFRONT"]

MANAGED_GROUPS = (
    "AWSManagedRulesCommonRuleSet",
    "AWSManagedRulesKnownBadInputsRuleSet",
)


def acl_name(d: Deployment, scope: Scope) -> str:
    return f"{d.name}-{'edge' if scope == 'CLOUDFRONT' else 'alb'}"


def needs_edge_waf(d: Deployment) -> bool:
    """True when a web ACL is created for CloudFront."""
    if d.ingress.kind != "cloudfront":
        return False
    return d.ingress.cloudfront.waf.kind == "managed" or d.auth.mode == "ip-allowlist"


def rule_names(d: Deployment, scope: Scope) -> list[str]:
    """The rules a created web ACL holds, in priority order."""
    names: list[str] = []
    if d.n8n.metrics or d.collects_metrics:
        names.append("HideMetrics")
    if scope == "CLOUDFRONT" and d.auth.mode == "ip-allowlist":
        names.append("AllowList")
    waf = d.ingress.cloudfront.waf if scope == "CLOUDFRONT" else d.ingress.waf
    if waf.kind == "managed":
        names.append("RateLimit")
        names.extend(MANAGED_GROUPS)
    return names


def _visibility(name: str) -> wafv2.CfnWebACL.VisibilityConfigProperty:
    return wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True,
        metric_name=name,
        sampled_requests_enabled=True,
    )


def _path(pattern: str) -> wafv2.CfnWebACL.StatementProperty:
    """A path pattern of the plan (``/webhook/*``, ``/healthz*``) as a match."""
    wildcard = pattern.endswith("*")
    return wafv2.CfnWebACL.StatementProperty(
        byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
            field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(uri_path={}),
            positional_constraint="STARTS_WITH" if wildcard else "EXACTLY",
            search_string=pattern.rstrip("*"),
            text_transformations=[
                wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="NONE")
            ],
        )
    )


def _any(statements: list[Any]) -> Any:
    if len(statements) == 1:
        return statements[0]
    return wafv2.CfnWebACL.StatementProperty(
        or_statement=wafv2.CfnWebACL.OrStatementProperty(statements=statements)
    )


def _not(statement: Any) -> Any:
    return wafv2.CfnWebACL.StatementProperty(
        not_statement=wafv2.CfnWebACL.NotStatementProperty(statement=statement)
    )


def _allow_list(
    scope: Construct, d: Deployment, kind: Scope
) -> wafv2.CfnWebACL.StatementProperty:
    """Block other addresses, except on the paths external callers use."""
    networks = [ipaddress.ip_network(c, strict=False) for c in d.ingress.allowed_cidrs]
    matches = []
    for version, label in ((4, "IPV4"), (6, "IPV6")):
        cidrs = [str(n) for n in networks if n.version == version]
        if not cidrs:
            continue
        ip_set = wafv2.CfnIPSet(
            scope,
            f"AllowedAddresses{version}",
            name=f"{acl_name(d, kind)}-allowed-ipv{version}",
            scope=kind,
            ip_address_version=label,
            addresses=cidrs,
        )
        matches.append(
            wafv2.CfnWebACL.StatementProperty(
                ip_set_reference_statement=wafv2.CfnWebACL.IPSetReferenceStatementProperty(
                    arn=ip_set.attr_arn
                )
            )
        )
    public = [_path(p) for p in PUBLIC_PATHS + d.auth.public_paths]
    return wafv2.CfnWebACL.StatementProperty(
        and_statement=wafv2.CfnWebACL.AndStatementProperty(
            statements=[_not(_any(matches)), _not(_any(public))]
        )
    )


def web_acl(scope: Construct, d: Deployment, kind: Scope) -> wafv2.CfnWebACL:
    """The web ACL of the deployment for ``kind`` (ALB or CloudFront)."""
    waf = d.ingress.cloudfront.waf if kind == "CLOUDFRONT" else d.ingress.waf
    rules: list[wafv2.CfnWebACL.RuleProperty] = []

    def add(name: str, statement: Any = None, **options: Any) -> None:
        rules.append(
            wafv2.CfnWebACL.RuleProperty(
                name=name,
                priority=len(rules),
                statement=statement,
                visibility_config=_visibility(name),
                **options,
            )
        )

    block = wafv2.CfnWebACL.RuleActionProperty(block={})
    for name in rule_names(d, kind):
        if name == "HideMetrics":
            add(
                name,
                wafv2.CfnWebACL.StatementProperty(
                    byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                        field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                            uri_path={}
                        ),
                        positional_constraint="STARTS_WITH",
                        search_string="/metrics",
                        text_transformations=[
                            wafv2.CfnWebACL.TextTransformationProperty(
                                priority=0, type="LOWERCASE"
                            )
                        ],
                    )
                ),
                action=block,
            )
        elif name == "AllowList":
            add(name, _allow_list(scope, d, kind), action=block)
        elif name == "RateLimit":
            assert waf.kind == "managed"
            add(
                name,
                wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=waf.rate_limit, aggregate_key_type="IP"
                    )
                ),
                action=block,
            )
        else:
            extra: dict[str, Any] = {}
            if name == "AWSManagedRulesCommonRuleSet":
                # n8n webhooks carry large bodies: count, don't block, on size.
                extra["rule_action_overrides"] = [
                    wafv2.CfnWebACL.RuleActionOverrideProperty(
                        name="SizeRestrictions_BODY",
                        action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
                    )
                ]
            add(
                name,
                wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS", name=name, **extra
                    )
                ),
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            )
    return wafv2.CfnWebACL(
        scope,
        "EdgeFirewall" if kind == "CLOUDFRONT" else "Firewall",
        name=acl_name(d, kind),
        scope=kind,
        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
        visibility_config=_visibility(acl_name(d, kind)),
        rules=rules,
    )
