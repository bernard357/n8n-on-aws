"""The questions of ``n8n-on-aws init``, in the order of the decision tree.

The flow builds the same dict a user would write by hand, with a comment for each
key it sets. Profile defaults are resolved as the flow goes, so each question
offers the profile's choice first and only asks what the earlier answers allow.
"""

from __future__ import annotations

import ipaddress
import math
import re
from typing import Any

from ..config import licensing as lic
from ..config import resolve
from ..config.models import Deployment
from ..config.profiles import PROFILES
from ..plan import build_plan
from .lookups import Lookups
from .prompts import Aborted, Choice, Prompter

OTHER = "(other)"
NAME = re.compile(r"^[a-z][a-z0-9-]{1,24}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
DOMAIN = re.compile(r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?\.)+[a-z]{2,}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERSION = re.compile(r"^(stable|latest|\d+\.\d+\.\d+)$")

PROFILE_SHAPES = {
    "nano": "one Spot task, SQLite on EFS; no ALB, no NAT",
    "small": "one task, Aurora Serverless, CloudFront; no ALB, no NAT",
    "medium": "queue mode with Spot workers, ALB, ElastiCache Serverless, one NAT",
    "large": "main, webhook processors and workers; NAT per AZ; Aurora with a reader",
    "xlarge": "multi-main HA (Enterprise), provisioned Aurora, node-based cache",
}

EDITIONS = [
    Choice(
        value="community",
        label="Community: free, Sustainable Use License (internal or personal use)",
    ),
    Choice(
        value="registered-community",
        label="Registered community: free, a few extra features once registered",
    ),
    Choice(value="business", label="Business: paid licence, SSO, S3 binary data"),
    Choice(value="enterprise", label="Enterprise: paid licence, multi-main HA"),
]

PURPOSES = [
    Choice(
        value="internal-business",
        label="My organisation's staff, for our own processes",
    ),
    Choice(value="personal", label="Just me"),
    Choice(value="non-commercial", label="A non-profit or hobby project"),
    Choice(
        value="service-for-third-parties",
        label="Our customers or other third parties, or we charge for access",
    ),
    Choice(value="embedded-editor", label="n8n's editor shown inside our product"),
]


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _check_cidrs(text: str) -> str | None:
    if not _csv(text):
        return "give at least one range, e.g. 203.0.113.0/24"
    for part in _csv(text):
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            return f"{part} is not an address range"
    return None


def _check_emails(text: str) -> str | None:
    bad = [e for e in _csv(text) if not EMAIL.match(e)]
    return f"not an email: {', '.join(bad)}" if bad else None


class Flow:
    """Asks the questions and collects the configuration with its comments."""

    def __init__(self, ask: Prompter, lookups: Lookups) -> None:
        self.ask = ask
        self.lookups = lookups
        self.data: dict[str, Any] = {"apiVersion": "n8n-on-aws/v1"}
        self.comments: dict[str, str] = {}

    # ---------------------------------------------------------- plumbing

    def set(self, path: str, value: Any, comment: str = "") -> None:
        node = self.data
        keys = path.split(".")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
        if comment:
            self.comments[path] = comment

    def resolved(self) -> Deployment:
        """The deployment as the answers so far resolve, profile included."""
        result = resolve(self.data)
        assert result.deployment is not None, result.findings
        return result.deployment

    def estimate(self, data: dict[str, Any] | None = None) -> tuple[float, float]:
        from ..finops.estimate import estimate

        result = resolve(data if data is not None else self.data)
        if result.deployment is None:
            return 0.0, 0.0
        cost = estimate(result.deployment, build_plan(result.deployment))
        return cost.total, cost.fixed

    def pick(
        self,
        key: str,
        question: str,
        found: list[tuple[str, str]],
        default: str = "",
        check: Any = None,
    ) -> str:
        """Choose from a lookup, or type the value when nothing was found."""
        if found:
            choices = [Choice(value=v, label=label) for v, label in found]
            choices.append(Choice(value=OTHER, label="another value"))
            answer = self.ask.choose(key, question, choices, found[0][0])
            if answer != OTHER:
                return answer
        return self.ask.text(f"{key}.text", question, default, check)

    # ------------------------------------------------------------- steps

    def run(self) -> dict[str, Any]:
        self.identity()
        self.size()
        self.edition()
        self.environment()
        self.platform()
        self.network()
        self.database()
        self.queue()
        self.files()
        self.ingress()
        self.domain()
        self.wall()
        self.budget()
        return self.data

    def identity(self) -> None:
        self.ask.say("n8n-on-aws init: a few questions, then a commented n8n.yaml.")
        name = self.ask.text(
            "name",
            "Deployment name (prefix of every AWS resource)",
            "n8n",
            lambda v: None if NAME.match(v) else "2-25 lowercase letters, digits, -",
        )
        self.set("name", name)
        region = self.ask.text(
            "region",
            "AWS region",
            self.lookups.default_region() or "eu-west-1",
            lambda v: None if REGION.match(v) else "a region such as eu-west-3",
        )
        self.set("aws.region", region, "where the stacks are deployed")
        version = self.ask.text(
            "version",
            "n8n version (pin a release such as 2.48.6 in production)",
            "stable",
            lambda v: None if VERSION.match(v) else "stable, latest or x.y.z",
        )
        self.set("n8n.version", version, "image tag of n8n and of its task runners")

    def size(self) -> None:
        choices = []
        for name in PROFILES:
            sample = dict(self.data, profile=name)
            if name == "xlarge":
                sample = dict(
                    sample,
                    n8n={"license": {"edition": "enterprise"}},
                )
            total, _ = self.estimate(sample)
            choices.append(
                Choice(
                    value=name,
                    label=f"{name:<7} {PROFILE_SHAPES[name]} · "
                    f"about ${total:,.0f}/month",
                )
            )
        profile = self.ask.choose(
            "profile",
            "How big? (estimates with the profile's default traffic)",
            choices,
            "small",
        )
        self.set("profile", profile, PROFILE_SHAPES[profile])

    def edition(self) -> None:
        if self.data["profile"] == "xlarge":
            self.ask.say("The xlarge profile runs multi-main, an Enterprise feature.")
            edition = "enterprise"
        else:
            edition = self.ask.choose(
                "edition", "Which n8n edition will you run?", EDITIONS, "community"
            )
        purpose = self.ask.choose(
            "purpose", "Who will use this n8n instance?", PURPOSES, "internal-business"
        )
        if edition in lic.COMMUNITY_EDITIONS and purpose not in lic.COMMUNITY_PURPOSES:
            self.ask.say(
                "This use is outside the Sustainable Use License: it needs a paid "
                "edition and terms agreed with n8n."
            )
            edition = self.ask.choose(
                "edition.paid", "Which paid edition?", EDITIONS[2:], "business"
            )
        self.set("n8n.license.edition", edition, "n8n edition, checked by the L rules")
        self.set("n8n.license.usage.purpose", purpose, "declared use of this instance")
        if edition in lic.COMMUNITY_EDITIONS:
            accepted = self.ask.confirm(
                "accept",
                f"Do you accept the Sustainable Use License ({lic.LICENSE_URL})?",
                False,
            )
            if not accepted:
                raise Aborted("community editions need the Sustainable Use License")
            self.set(
                "n8n.license.usage.accept_sustainable_use_license",
                True,
                "accepted when this file was written",
            )
        if edition == "business":
            employees = self.ask.text(
                "employees",
                "How many employees does your company have?",
                "50",
                lambda v: None if v.isdigit() and int(v) > 0 else "a whole number",
            )
            self.set(
                "n8n.license.usage.employees",
                int(employees),
                "n8n positions Business below a size",
            )
        if edition != "community":
            region = self.data["aws"]["region"]
            secret = self.pick(
                "license_secret",
                "Secrets Manager secret holding the licence activation key",
                [(s, s) for s in self.lookups.secrets(region)],
                "n8n/license",
                lambda v: None if v else "a secret name",
            )
            self.set(
                "n8n.license.activation_key",
                {"secret": secret},
                "read by the tasks; never stored in this file",
            )

    def environment(self) -> None:
        environment = self.ask.choose(
            "environment",
            "Environment",
            [
                Choice(value="production", label="production"),
                Choice(value="staging", label="staging"),
                Choice(value="dev", label="dev (data deleted with the stacks)"),
            ],
            "production",
        )
        self.set("environment", environment, "tags, protections and FinOps advice")
        if environment != "production" and self.ask.confirm(
            "office_hours",
            "Stop n8n outside office hours (Monday to Friday, 08:00-19:00 UTC)?",
            environment == "dev",
        ):
            self.set(
                "finops.schedule.kind",
                "office-hours",
                "services scale to zero at night and on weekends",
            )

    def platform(self) -> None:
        platform = self.ask.choose(
            "platform",
            "Where should n8n run?",
            [
                Choice(value="ecs", label="Amazon ECS on Fargate (recommended)"),
                Choice(
                    value="eks",
                    label="Amazon EKS: planned (#11), validates and plans only",
                ),
            ],
            "ecs",
        )
        if platform != "eks":
            return
        self.set("runtime.platform", "eks", "EKS is planned: validate and plan only")
        region = self.data["aws"]["region"]
        clusters = self.lookups.eks_clusters(region)
        choices = [Choice(value="(create)", label="create a new cluster")] + [
            Choice(value=c, label=f"existing cluster {c}") for c in clusters
        ]
        cluster = self.ask.choose("eks_cluster", "EKS cluster", choices, "(create)")
        if cluster == "(create)" and not clusters:
            name = self.ask.text(
                "eks_cluster.text", "Existing cluster name (empty to create one)"
            )
            cluster = name or "(create)"
        if cluster != "(create)":
            self.set(
                "runtime.eks.cluster",
                {"kind": "existing", "name": cluster},
                "n8n is installed in this cluster",
            )

    def network(self) -> None:
        region = self.data["aws"]["region"]
        mode = self.ask.choose(
            "vpc",
            "Network",
            [
                Choice(value="create", label="create a new VPC"),
                Choice(value="existing", label="use an existing VPC"),
            ],
            "create",
        )
        if mode == "create":
            deployment = self.resolved()
            created = deployment.network.vpc
            if (
                deployment.runtime.platform == "eks"
                and created.kind == "create"
                and created.nat == "none"
            ):
                self.set(
                    "network.vpc.nat",
                    "single",
                    "EKS nodes run in private subnets and need a NAT",
                )
            return
        vpc_id = self.pick(
            "vpc_id",
            "Which VPC?",
            self.lookups.vpcs(region),
            check=lambda v: None if v.startswith("vpc-") else "a VPC id (vpc-...)",
        )
        subnets = self.lookups.subnets(region, vpc_id)
        private = _one_per_zone([s for s in subnets if not s.public])
        public = _one_per_zone([s for s in subnets if s.public])
        if len(private) >= 2 and self.ask.confirm(
            "subnets",
            "Use private subnets "
            + ", ".join(f"{s.id} ({s.zone})" for s in private)
            + (
                " and public subnets " + ", ".join(s.id for s in public)
                if public
                else ""
            )
            + "?",
            True,
        ):
            zones = [s.zone for s in private]
            private_ids = [s.id for s in private]
            public_ids = [s.id for s in public]
        else:
            private_ids = _csv(
                self.ask.text(
                    "private_subnets",
                    "Private subnets, one per AZ (comma-separated)",
                    check=lambda v: None if len(_csv(v)) >= 2 else "at least 2",
                )
            )
            zones = _csv(
                self.ask.text(
                    "zones",
                    "Their availability zones, in the same order",
                    check=lambda v: (
                        None
                        if len(_csv(v)) == len(private_ids)
                        else f"{len(private_ids)} zones"
                    ),
                )
            )
            public_ids = _csv(
                self.ask.text(
                    "public_subnets",
                    "Public subnets (empty if none; an internet-facing ALB or "
                    "CloudFront to the task needs 2)",
                )
            )
        vpc: dict[str, Any] = {
            "kind": "existing",
            "id": vpc_id,
            "availability_zones": zones,
            "private_subnets": private_ids,
        }
        if public_ids:
            vpc["public_subnets"] = public_ids
        self.set(
            "network.vpc", vpc, "an existing VPC; nothing is created in it but SGs"
        )

    def database(self) -> None:
        kind = self.resolved().database.kind
        mode = self.ask.choose(
            "database",
            "Database",
            [
                Choice(value="create", label=f"create one: {kind} (profile default)"),
                Choice(
                    value="external",
                    label="use an existing PostgreSQL (Aurora, RDS or elsewhere)",
                ),
            ],
            "create",
        )
        if mode == "create":
            return
        region = self.data["aws"]["region"]
        host = self.pick(
            "database_host",
            "PostgreSQL host",
            self.lookups.databases(region),
            check=lambda v: None if v else "a host name",
        )
        secret = self.pick(
            "database_secret",
            "Secrets Manager secret with its username and password",
            [(s, s) for s in self.lookups.secrets(region)],
            f"{self.data['name']}/database",
            lambda v: None if v else "a secret name",
        )
        self.set(
            "database",
            {"kind": "external", "host": host, "credentials": {"secret": secret}},
            "an existing PostgreSQL; the secret holds username and password",
        )

    def queue(self) -> None:
        deployment = self.resolved()
        if not deployment.is_queued:
            return
        mode = self.ask.choose(
            "queue",
            "Queue (Redis protocol, for the workers)",
            [
                Choice(
                    value="create",
                    label=f"create one: {deployment.queue.kind} (profile default)",
                ),
                Choice(value="external", label="use an existing Redis or Valkey"),
            ],
            "create",
        )
        if mode == "create":
            return
        region = self.data["aws"]["region"]
        host = self.pick(
            "queue_host",
            "Redis host",
            self.lookups.caches(region),
            check=lambda v: None if v else "a host name",
        )
        queue: dict[str, Any] = {"kind": "external", "host": host}
        if not self.ask.confirm("queue_tls", "Does it use TLS?", True):
            queue["tls"] = False
        auth = self.ask.text(
            "queue_auth", "Secret with its AUTH token (empty if none)", ""
        )
        if auth:
            queue["auth"] = {"secret": auth}
        self.set("queue", queue, "an existing Redis-compatible endpoint")

    def files(self) -> None:
        edition = self.data["n8n"]["license"]["edition"]
        if not lic.allows(edition, "s3-binary-data"):
            return
        if self.ask.confirm(
            "s3_binary",
            "Keep the files that workflows handle in S3 (instead of the database)?",
            False,
        ):
            self.set("storage.binary_data", "s3", "binary data in S3, a paid feature")
            self.set("storage.bucket", {"kind": "create"}, "a bucket for n8n's files")

    def ingress(self) -> None:
        deployment = self.resolved()
        if deployment.runtime.platform == "eks":
            self.set("ingress.kind", "alb", "EKS is reached through an ALB")
            return
        kind = self.ask.choose(
            "ingress",
            "How do users reach n8n?",
            [
                Choice(
                    value="cloudfront",
                    label="Amazon CloudFront: cheapest, no fixed floor, edge TLS",
                ),
                Choice(
                    value="alb",
                    label="Application Load Balancer: private networks, ~$20+/month",
                ),
            ],
            deployment.ingress.kind,
        )
        self.set(
            "ingress.kind",
            kind,
            "CloudFront in front of n8n"
            if kind == "cloudfront"
            else "an ALB in front of n8n",
        )
        if kind == "cloudfront" and deployment.topology.mode in ("split", "multi-main"):
            self.set(
                "ingress.cloudfront.origin",
                "alb",
                "CloudFront to the task cannot route webhook processors",
            )
        if kind == "alb" and not self.ask.confirm(
            "public", "Reachable from the internet?", True
        ):
            self.set("ingress.public", False, "internal ALB, for VPN or private users")

    def domain(self) -> None:
        deployment = self.resolved()
        eks = deployment.runtime.platform == "eks"
        zones = self.lookups.zones()
        if eks:
            self.ask.say(
                "EKS needs a domain: the ALB host name is not known in advance."
            )
        elif not self.ask.confirm(
            "use_domain",
            "Serve n8n on a domain of yours (recommended for stable webhook URLs)?",
            bool(zones),
        ):
            return
        zone = self.pick(
            "zone",
            "Route 53 hosted zone (empty if the domain is not in Route 53)",
            [(f"{z.id} {z.name}", z.name) for z in zones],
            "",
        )
        zone_id, _, zone_name = (
            zone.partition(" ")
            if zone.startswith("Z")
            else (
                "",
                "",
                zone,
            )
        )
        zone_name = zone_name.strip().rstrip(".")
        domain = self.ask.text(
            "domain",
            "Domain name of n8n",
            f"n8n.{zone_name}" if zone_name else "",
            lambda v: _check_domain(v, zone_name),
        )
        self.set("ingress.domain", domain, "editor and webhook URLs")
        if zone_id:
            self.set(
                "ingress.hosted_zone",
                {"kind": "existing", "id": zone_id, "name": zone_name},
                "DNS record and certificate validation",
            )
        elif zone_name:
            self.set(
                "ingress.hosted_zone",
                {"kind": "lookup", "name": zone_name},
                "looked up at synthesis",
            )
        create = (
            bool(zone_name)
            and self.ask.choose(
                "certificate",
                "TLS certificate",
                [
                    Choice(value="create", label="create one (ACM, validated by DNS)"),
                    Choice(value="existing", label="use an existing ACM certificate"),
                ],
                "create",
            )
            == "create"
        )
        if create:
            self.set("ingress.certificate", {"kind": "create"}, "ACM, DNS-validated")
            return
        region = (
            "us-east-1"
            if self.data["ingress"]["kind"] == "cloudfront"
            else self.data["aws"]["region"]
        )
        certificates = [
            (c.arn, c.domain)
            for c in self.lookups.certificates(region)
            if _covers(c.domain, domain)
        ]
        arn = self.pick(
            "certificate_arn",
            f"ACM certificate for {domain} (in {region})",
            certificates,
            check=lambda v: None if v.startswith("arn:aws") else "an ACM ARN",
        )
        self.set(
            "ingress.certificate",
            {"kind": "existing", "arn": arn},
            "CloudFront needs a us-east-1 certificate" if region == "us-east-1" else "",
        )

    def wall(self) -> None:
        deployment = self.resolved()
        ingress = deployment.ingress
        has_domain = bool(ingress.domain)
        choices = [
            Choice(value="none", label="none: n8n's own login only"),
            Choice(value="ip-allowlist", label="an IP allow-list"),
        ]
        cognito = (
            "cloudfront-cognito" if ingress.kind == "cloudfront" else "alb-cognito"
        )
        if has_domain and ingress.public:
            choices.append(
                Choice(
                    value=cognito,
                    label="Amazon Cognito sign-in before the editor "
                    "(webhooks stay open)",
                )
            )
        else:
            self.ask.say("A Cognito login wall needs a public domain; not offered.")
        self.ask.say("ALB OIDC (Entra ID, Okta, Google) is set by hand: see docs.")
        mode = self.ask.choose("wall", "Login wall in front of n8n", choices, "none")
        if mode != "none":
            self.set(
                "auth.mode",
                mode,
                "IP allow-list"
                if mode == "ip-allowlist"
                else "Cognito sign-in before the editor; webhooks stay open",
            )
        if mode == "ip-allowlist":
            cidrs = _csv(
                self.ask.text(
                    "cidrs",
                    "Address ranges allowed (comma-separated)",
                    check=_check_cidrs,
                )
            )
            note = (
                "only these reach the editor; webhooks stay open"
                if ingress.kind == "cloudfront"
                else "ALB security group: every path, webhooks included"
            )
            self.set("ingress.allowed_cidrs", cidrs, note)
        if mode == cognito:
            mfa = self.ask.choose(
                "mfa",
                "Multi-factor authentication",
                [
                    Choice(value="optional", label="optional"),
                    Choice(value="required", label="required"),
                    Choice(value="off", label="off"),
                ],
                "optional",
            )
            invite = _csv(
                self.ask.text(
                    "invite",
                    "Emails to invite (comma-separated, empty for none)",
                    check=_check_emails,
                )
            )
            spec: dict[str, Any] = {"kind": "create", "mfa": mfa}
            if invite:
                spec["invite"] = invite
            self.set("auth.cognito", spec, "a new user pool")
        if self.ask.confirm(
            "waf",
            "Add AWS WAF (rate limit and managed rules, about $8/month plus "
            "$0.60 per million requests)?",
            False,
        ):
            path = (
                "ingress.cloudfront.waf"
                if ingress.kind == "cloudfront"
                else "ingress.waf"
            )
            self.set(path, {"kind": "managed"}, "rate limit and AWS managed rules")

    def budget(self) -> None:
        total, fixed = self.estimate()
        self.ask.say(
            f"Estimated cost: about ${total:,.0f}/month (${fixed:,.0f} fixed), "
            "before taxes; details with `plan --cost`."
        )
        suggested = max(10, math.ceil(total * 1.25 / 10) * 10)
        amount = self.ask.text(
            "budget",
            "Monthly budget in USD, alerts at 80% and 100% (none to skip)",
            str(suggested),
            lambda v: None if v == "none" or _number(v) else "a number, or none",
        )
        if amount == "none":
            return
        self.set(
            "finops.budget.monthly_usd",
            float(amount),
            "AWS Budgets alerts at 80% and 100%",
        )
        emails = _csv(
            self.ask.text(
                "budget_emails",
                "Who gets the budget alerts? (comma-separated)",
                check=lambda v: (
                    "at least one email" if not _csv(v) else _check_emails(v)
                ),
            )
        )
        self.set("finops.budget.alert_emails", emails, "")
        if self.ask.confirm(
            "ceiling",
            "Also refuse to deploy when the estimate is above this budget?",
            False,
        ):
            self.set(
                "finops.max_monthly_usd",
                float(amount),
                "plan and deploy stop above this estimate",
            )


def _number(text: str) -> bool:
    try:
        return float(text) > 0
    except ValueError:
        return False


def _check_domain(domain: str, zone: str) -> str | None:
    if not DOMAIN.match(domain):
        return "a domain name such as n8n.example.com"
    if zone and not (domain == zone or domain.endswith("." + zone)):
        return f"the domain must be in {zone}"
    return None


def _covers(certificate: str, domain: str) -> bool:
    if certificate == domain:
        return True
    return certificate.startswith("*.") and domain.endswith(certificate[1:])


def _one_per_zone(subnets: list[Any]) -> list[Any]:
    seen: dict[str, Any] = {}
    for subnet in subnets:
        seen.setdefault(subnet.zone, subnet)
    return list(seen.values())[:3]
