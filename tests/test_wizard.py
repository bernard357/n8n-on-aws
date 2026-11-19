"""`n8n-on-aws init`: the question flow, driven with scripted answers."""

from __future__ import annotations

import io
import json
import subprocess
from typing import Any

import pytest
import yaml

from n8n_on_aws import cli
from n8n_on_aws.config import resolve
from n8n_on_aws.plan import build_plan
from n8n_on_aws.wizard import Aborted, Lookups, ScriptedPrompter, interview
from n8n_on_aws.wizard.emit import _leaves, _signature, _without

OFFLINE = Lookups(offline=True)
COMMUNITY = {
    "name": "n8n-test",
    "region": "eu-west-3",
    "accept": True,
    "budget": "none",
}


def run(answers: dict[str, Any], lookups: Lookups = OFFLINE):  # type: ignore[no-untyped-def]
    ask = ScriptedPrompter(answers)
    draft = interview(ask, lookups)
    assert draft.ok, [f for f in draft.result.findings if f.level == "error"]
    # The written text is what validate reads: same deployment, same plan.
    reread = resolve(yaml.safe_load(draft.text))
    assert reread.ok and reread.deployment is not None
    assert reread.deployment == draft.result.deployment
    assert build_plan(reread.deployment) == build_plan(draft.result.deployment)
    return draft, ask


# ---------------------------------------------------------------- paths


def test_defaults_give_the_smallest_file() -> None:
    draft, _ = run(COMMUNITY)
    assert draft.data == {
        "apiVersion": "n8n-on-aws/v1",
        "name": "n8n-test",
        "aws": {"region": "eu-west-3"},
        "profile": "small",
        "n8n": {
            "license": {
                "usage": {
                    "purpose": "internal-business",
                    "accept_sustainable_use_license": True,
                }
            },
        },
    }
    assert "# where the stacks are deployed" in draft.text
    assert draft.text.startswith("# yaml-language-server: $schema=")


def test_every_written_key_is_a_real_choice() -> None:
    draft, _ = run(
        {
            **COMMUNITY,
            "profile": "medium",
            "environment": "dev",
            "office_hours": True,
            "budget": "300",
            "budget_emails": "ops@example.com",
        }
    )
    reference = _signature(draft.data)
    for path in _leaves(draft.data):
        if path[0] in ("apiVersion", "name", "profile"):
            continue
        assert _signature(_without(draft.data, path)) != reference, path


def test_declining_the_sustainable_use_license_stops() -> None:
    with pytest.raises(Aborted):
        interview(ScriptedPrompter({**COMMUNITY, "accept": False}), OFFLINE)


def test_third_party_use_moves_to_a_paid_edition() -> None:
    draft, ask = run(
        {
            **COMMUNITY,
            "purpose": "service-for-third-parties",
            "edition.paid": "business",
            "employees": "40",
            "license_secret.text": "n8n/license",
        }
    )
    license = draft.data["n8n"]["license"]
    assert license["edition"] == "business"
    assert license["activation_key"] == {"secret": "n8n/license"}
    assert license["usage"]["employees"] == 40
    assert "accept" not in ask.asked
    assert any("outside the Sustainable Use License" in s for s in ask.said)


def test_enterprise_xlarge_with_s3_alb_domain_cognito_waf_and_ceiling() -> None:
    draft, ask = run(
        {
            **COMMUNITY,
            "profile": "xlarge",
            "license_secret.text": "n8n/license",
            "s3_binary": True,
            "ingress": "alb",
            "use_domain": True,
            "zone.text": "example.com",
            "domain": "flows.example.com",
            "wall": "alb-cognito",
            "mfa": "required",
            "invite": "alice@example.com, bob@example.com",
            "waf": True,
            "budget": "3000",
            "budget_emails": "finops@example.com",
            "ceiling": True,
        }
    )
    assert "edition" not in ask.asked  # xlarge implies Enterprise
    d = draft.result.deployment
    assert d is not None
    assert d.n8n.license.edition == "enterprise"
    assert d.binary_mode == "s3" and d.storage.bucket.kind == "create"
    assert d.ingress.hosted_zone.kind == "lookup"
    assert d.auth.mode == "alb-cognito"
    assert d.auth.cognito.kind == "create" and d.auth.cognito.mfa == "required"
    assert d.ingress.waf.kind == "managed"
    assert d.finops.max_monthly_usd == 3000


def test_cloudfront_with_split_topology_uses_the_alb_origin() -> None:
    draft, _ = run({**COMMUNITY, "profile": "large", "ingress": "cloudfront"})
    d = draft.result.deployment
    assert d is not None and d.ingress.cloudfront.origin == "alb"


def test_cloudfront_login_wall_and_allow_list() -> None:
    edge, _ = run(
        {
            **COMMUNITY,
            "use_domain": True,
            "zone.text": "example.com",
            "wall": "cloudfront-cognito",
            "waf": True,
        }
    )
    d = edge.result.deployment
    assert d is not None
    assert d.auth.mode == "cloudfront-cognito" and d.ingress.domain == "n8n.example.com"
    assert d.ingress.cloudfront.waf.kind == "managed"
    allow, _ = run({**COMMUNITY, "wall": "ip-allowlist", "cidrs": "203.0.113.0/24"})
    d = allow.result.deployment
    assert d is not None and d.ingress.allowed_cidrs == ["203.0.113.0/24"]


def test_no_cognito_without_a_domain() -> None:
    _, ask = run(COMMUNITY)
    with pytest.raises(ValueError, match="not one of the choices"):
        interview(
            ScriptedPrompter({**COMMUNITY, "wall": "cloudfront-cognito"}), OFFLINE
        )
    assert any("needs a public domain" in s for s in ask.said)


def test_eks_needs_a_domain_an_alb_and_a_nat() -> None:
    draft, ask = run(
        {
            **COMMUNITY,
            "platform": "eks",
            "zone.text": "example.com",
        }
    )
    d = draft.result.deployment
    assert d is not None
    assert d.runtime.platform == "eks" and d.ingress.kind == "alb"
    assert d.network.vpc.kind == "create" and d.network.vpc.nat != "none"
    assert d.ingress.domain == "n8n.example.com"
    assert "use_domain" not in ask.asked and "ingress" not in ask.asked


def test_existing_vpc_database_and_redis_typed_offline() -> None:
    draft, _ = run(
        {
            **COMMUNITY,
            "profile": "medium",
            "vpc": "existing",
            "vpc_id.text": "vpc-0abc",
            "private_subnets": "subnet-1, subnet-2",
            "zones": "eu-west-3a, eu-west-3b",
            "public_subnets": "subnet-3, subnet-4",
            "database": "external",
            "database_host.text": "db.internal",
            "database_secret.text": "prod/n8n-db",
            "queue": "external",
            "queue_host.text": "redis.internal",
            "queue_tls": False,
            "queue_auth": "prod/redis",
        }
    )
    d = draft.result.deployment
    assert d is not None
    assert d.network.vpc.kind == "existing"
    assert d.network.vpc.private_subnets == ["subnet-1", "subnet-2"]
    assert d.database.kind == "external" and d.database.host == "db.internal"
    assert d.queue.kind == "external" and d.queue.tls is False


def test_existing_certificate_for_cloudfront_is_looked_up_in_us_east_1() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload: dict[str, Any] = {}
        if "list-hosted-zones" in command:
            payload = {
                "HostedZones": [
                    {"Id": "/hostedzone/Z0ABC", "Name": "example.com.", "Config": {}}
                ]
            }
        if "list-certificates" in command:
            payload = {
                "CertificateSummaryList": [
                    {
                        "CertificateArn": "arn:aws:acm:us-east-1:1:certificate/x",
                        "DomainName": "*.example.com",
                    }
                ]
            }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    draft, _ = run(
        {
            **COMMUNITY,
            "use_domain": True,
            "zone": "Z0ABC example.com",
            "certificate": "existing",
            "certificate_arn": "arn:aws:acm:us-east-1:1:certificate/x",
        },
        Lookups(runner=runner),
    )
    d = draft.result.deployment
    assert d is not None
    assert d.ingress.hosted_zone.kind == "existing"
    assert d.ingress.certificate.kind == "existing"
    acm = next(c for c in calls if "list-certificates" in c)
    assert acm[acm.index("--region") + 1] == "us-east-1"


# -------------------------------------------------------------- lookups


def test_lookups_parse_the_aws_cli_and_go_quiet_offline() -> None:
    replies = {
        "describe-vpcs": {
            "Vpcs": [
                {
                    "VpcId": "vpc-1",
                    "CidrBlock": "10.0.0.0/16",
                    "Tags": [{"Key": "Name", "Value": "main"}],
                }
            ]
        },
        "describe-subnets": {
            "Subnets": [
                {"SubnetId": "s-a", "AvailabilityZone": "eu-west-3a"},
                {"SubnetId": "s-b", "AvailabilityZone": "eu-west-3b"},
                {
                    "SubnetId": "p-a",
                    "AvailabilityZone": "eu-west-3a",
                    "MapPublicIpOnLaunch": True,
                },
            ]
        },
        "list-secrets": {"SecretList": [{"Name": "b"}, {"Name": "a"}]},
        "list-clusters": {"clusters": ["k8s"]},
        "describe-db-clusters": {
            "DBClusters": [
                {
                    "Endpoint": "c.rds",
                    "Engine": "aurora-postgresql",
                    "DBClusterIdentifier": "c",
                }
            ]
        },
        "describe-db-instances": {"DBInstances": []},
        "describe-serverless-caches": {
            "ServerlessCaches": [
                {"ServerlessCacheName": "q", "Endpoint": {"Address": "q.cache"}}
            ]
        },
        "describe-replication-groups": {"ReplicationGroups": []},
    }

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        verb = next(v for v in replies if v in command)
        return subprocess.CompletedProcess(command, 0, json.dumps(replies[verb]), "")

    found = Lookups(runner=runner)
    assert found.vpcs("eu-west-3") == [("vpc-1", "vpc-1 main 10.0.0.0/16")]
    assert [s.id for s in found.subnets("eu-west-3", "vpc-1")] == ["s-a", "s-b", "p-a"]
    assert found.secrets("eu-west-3") == ["a", "b"]
    assert found.eks_clusters("eu-west-3") == ["k8s"]
    assert found.databases("eu-west-3") == [("c.rds", "c (aurora-postgresql)")]
    assert found.caches("eu-west-3") == [("q.cache", "q (serverless)")]
    assert OFFLINE.zones() == [] and OFFLINE.default_region() is None


# ------------------------------------------------------------------ cli


def _cli(
    argv: list[str], stdin: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    out = io.StringIO()
    return cli.main(argv, out), out.getvalue()


def test_cli_init_writes_a_valid_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "n8n.yaml"
    # name, region, version, size, edition, purpose, accept, environment,
    # platform, network, database, ingress, domain?, wall, waf?, budget
    answers = "demo\neu-west-3\n\n\n\n\ny\n\n\n\n\n\nn\n\nn\nnone\n"
    code, text = _cli(
        ["init", "--offline", "-o", str(target), "--yes"], answers, monkeypatch
    )
    assert code == 0, text
    assert "Monthly cost estimate" in text and "Wrote" in text
    assert cli.main(["validate", "-f", str(target)], io.StringIO()) == 0
    code, _ = _cli(["init", "--offline", "-o", str(target)], "", monkeypatch)
    assert code == 2  # never overwrites without --force


def test_cli_init_from_a_sample(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "team.yaml"
    code, text = _cli(
        ["init", "--offline", "--from", "small-team", "-o", str(target), "--yes"],
        "team\neu-west-1\n2.48.6\n",
        monkeypatch,
    )
    assert code == 0, text
    data = yaml.safe_load(target.read_text())
    assert data["name"] == "team" and data["aws"]["region"] == "eu-west-1"
    assert data["auth"]["mode"] == "cloudfront-cognito"
