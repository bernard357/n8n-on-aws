"""One failing input per rule, plus the passing counterpart where useful."""

from __future__ import annotations

from n8n_on_aws.config.rules import fargate_size_ok

from .conftest import check, codes

DOMAIN = {
    "domain": "n8n.example.com",
    "hosted_zone": {"kind": "lookup", "name": "example.com"},
    "certificate": {"kind": "create"},
}
BUSINESS = {
    "edition": "business",
    "activation_key": {"secret": "n8n/license"},
    "usage": {"purpose": "internal-business"},
}
ENTERPRISE = {**BUSINESS, "edition": "enterprise"}


def test_base_is_valid() -> None:
    assert check().ok


# ------------------------------------------------------------------ schema


def test_unknown_key_is_rejected() -> None:
    result = check({"topology": {"mode": "single", "flavour": "x"}})
    assert codes(result) == {"S1"}
    assert result.findings[0].path == "topology.flavour"


def test_key_of_another_kind_is_rejected() -> None:
    result = check({"database": {"kind": "rds", "min_acu": 1}})
    assert codes(result) == {"S1"}
    assert result.findings[0].path == "database.min_acu"


def test_plain_secret_is_rejected() -> None:
    result = check({"n8n": {"license": {"activation_key": "abc"}}})
    assert "S1" in codes(result)


# ---------------------------------------------------------------- topology


def test_t1_queue_needs_postgres() -> None:
    result = check({"topology": {"mode": "queue"}, "database": {"kind": "sqlite"}})
    assert "T1" in codes(result)


def test_t1_queue_needs_redis() -> None:
    result = check({"topology": {"mode": "queue"}, "queue": {"kind": "none"}})
    assert "T1" in codes(result)


def test_t2_single_runs_one_main() -> None:
    result = check({"topology": {"mode": "single", "main": {"replicas": 2}}})
    assert "T2" in codes(result)


def test_t2_unused_queue_is_a_warning() -> None:
    result = check({"queue": {"kind": "elasticache-serverless"}})
    assert result.ok
    assert "T2" in codes(result, "warning")


def test_t3_multi_main_needs_two_replicas() -> None:
    result = check(
        {
            "n8n": {"license": ENTERPRISE},
            "topology": {"mode": "multi-main", "main": {"replicas": 1}},
            "ingress": {"kind": "alb"},
        }
    )
    assert "T3" in codes(result)


def test_t3_queue_with_replicas_needs_multi_main() -> None:
    result = check({"topology": {"mode": "queue", "main": {"replicas": 2}}})
    assert "T3" in codes(result)


def test_t4_filesystem_binary_in_queue() -> None:
    result = check(
        {"topology": {"mode": "queue"}, "storage": {"binary_data": "filesystem"}}
    )
    assert "T4" in codes(result)


def test_t4_sqlite_on_eks() -> None:
    result = check(
        {
            "database": {"kind": "sqlite"},
            "runtime": {"platform": "eks"},
            "ingress": {"kind": "alb", **DOMAIN},
        }
    )
    assert "T4" in codes(result)


def test_t5_eks_needs_domain() -> None:
    result = check({"runtime": {"platform": "eks"}, "ingress": {"kind": "alb"}})
    assert "T5" in codes(result)


def test_t6_alb_auth_without_alb() -> None:
    result = check({"auth": {"mode": "alb-cognito"}, "ingress": DOMAIN})
    assert "T6" in codes(result)


def test_t6_alb_oidc_needs_oidc_block() -> None:
    result = check({"auth": {"mode": "alb-oidc"}, "ingress": {"kind": "alb", **DOMAIN}})
    assert "T6" in codes(result)


def test_t7_cloudfront_cognito_needs_cloudfront() -> None:
    result = check(
        {"auth": {"mode": "cloudfront-cognito"}, "ingress": {"kind": "alb", **DOMAIN}}
    )
    assert "T7" in codes(result)


def test_t8_task_origin_cannot_split() -> None:
    result = check(
        {
            "topology": {"mode": "split"},
            "ingress": {"kind": "cloudfront", "cloudfront": {"origin": "task"}},
        }
    )
    assert "T8" in codes(result)


def test_t8_split_on_small_profile_defaults_to_alb() -> None:
    result = check({"topology": {"mode": "split"}})
    assert result.ok, result.findings
    assert result.deployment is not None
    assert result.deployment.ingress.kind == "alb"


def test_t9_private_needs_alb() -> None:
    result = check({"ingress": {"public": False}})
    assert "T9" in codes(result)


def test_t9_domain_outside_zone() -> None:
    ingress = {**DOMAIN, "domain": "n8n.other.org"}
    result = check({"ingress": ingress})
    assert "T9" in codes(result)


def test_t9_domain_needs_certificate() -> None:
    result = check({"ingress": {"domain": "n8n.example.com"}})
    assert "T9" in codes(result)


def test_t10_existing_vpc_needs_public_subnets() -> None:
    vpc = {
        "kind": "existing",
        "id": "vpc-0abc",
        "availability_zones": ["x1a", "x1b"],
        "private_subnets": ["a", "b"],
    }
    result = check({"network": {"vpc": vpc}})
    assert "T10" in codes(result)


def test_t11_invalid_fargate_size() -> None:
    result = check({"topology": {"main": {"cpu": 512, "memory": 512}}})
    assert "T11" in codes(result)
    assert fargate_size_ok(1024, 2048)
    assert not fargate_size_ok(300, 1024)


def test_t12_s3_needs_bucket() -> None:
    result = check({"storage": {"exchange": {"enabled": True}}})
    assert "T12" in codes(result)


def test_t13_eks_needs_nat() -> None:
    result = check(
        {
            "runtime": {"platform": "eks"},
            "ingress": {"kind": "alb", **DOMAIN},
            "network": {"vpc": {"kind": "create", "nat": "none"}},
        }
    )
    assert "T13" in codes(result)


def test_t14_moving_tag_warns_and_old_version_fails() -> None:
    assert "T14" in codes(check({"n8n": {"version": "stable"}}), "warning")
    assert "T14" in codes(check({"n8n": {"version": "1.123.4"}}))


def test_t15_internal_runners_warn() -> None:
    result = check({"n8n": {"runners": {"mode": "internal"}}})
    assert "T15" in codes(result, "warning")


# ----------------------------------------------------------------- licence


def test_l1_community_needs_usage() -> None:
    result = check({"n8n": {"license": {"usage": None}}})
    assert "L1" in codes(result)


def test_l1_community_needs_acceptance() -> None:
    usage = {"purpose": "personal", "accept_sustainable_use_license": False}
    result = check({"n8n": {"license": {"usage": usage}}})
    assert "L1" in codes(result)


def test_l2_community_cannot_serve_third_parties() -> None:
    for purpose in ("service-for-third-parties", "embedded-editor"):
        result = check({"n8n": {"license": {"usage": {"purpose": purpose}}}})
        assert "L2" in codes(result), purpose


def test_l3_embedded_editor_needs_oem() -> None:
    license = {**BUSINESS, "usage": {"purpose": "embedded-editor"}}
    result = check({"n8n": {"license": license}})
    assert result.ok
    assert "L3" in codes(result, "warning")


def test_l4_service_for_third_parties_warns_on_paid() -> None:
    license = {**ENTERPRISE, "usage": {"purpose": "service-for-third-parties"}}
    assert "L4" in codes(check({"n8n": {"license": license}}), "warning")


def test_l5_business_for_large_companies() -> None:
    license = {**BUSINESS, "usage": {"purpose": "internal-business", "employees": 500}}
    assert "L5" in codes(check({"n8n": {"license": license}}), "warning")


def test_l6_paid_edition_needs_key() -> None:
    result = check({"n8n": {"license": {"edition": "business"}}})
    assert "L6" in codes(result)


def test_l6_key_and_certificate_are_exclusive() -> None:
    license = {**BUSINESS, "certificate": {"secret": "n8n/cert"}}
    assert "L6" in codes(check({"n8n": {"license": license}}))


def test_l7_multi_main_needs_enterprise() -> None:
    result = check({"topology": {"mode": "multi-main"}})
    assert "L7" in codes(result)
    licensed = check(
        {"topology": {"mode": "multi-main"}, "n8n": {"license": ENTERPRISE}}
    )
    assert licensed.ok, licensed.findings


def test_l7_s3_binary_data_needs_business() -> None:
    storage = {"bucket": {"kind": "create"}, "binary_data": "s3"}
    assert "L7" in codes(check({"storage": storage}))
    assert check({"storage": storage, "n8n": {"license": BUSINESS}}).ok


def test_l7_sso_needs_business() -> None:
    sso = {
        "kind": "oidc",
        "discovery_endpoint": "https://idp/.well-known/openid-configuration",
        "client_id": "n8n",
        "client_secret": {"secret": "n8n/oidc"},
    }
    assert "L7" in codes(check({"auth": {"n8n_sso": sso}}))


def test_l8_raw_env_cannot_bypass_gates() -> None:
    env = {"N8N_MULTI_MAIN_SETUP_ENABLED": "true", "N8N_DEFAULT_BINARY_DATA_MODE": "s3"}
    result = check({"n8n": {"env": env}})
    paths = {f.path for f in result.findings if f.code == "L7"}
    assert paths == {
        "n8n.env.N8N_MULTI_MAIN_SETUP_ENABLED",
        "n8n.env.N8N_DEFAULT_BINARY_DATA_MODE",
    }


def test_l8_licence_env_is_reserved() -> None:
    result = check({"n8n": {"env": {"N8N_LICENSE_ACTIVATION_KEY": "x"}}})
    assert "L8" in codes(result)


def test_l9_existing_vpc_reminds_licence_egress() -> None:
    vpc = {
        "kind": "existing",
        "id": "vpc-0abc",
        "availability_zones": ["x1a", "x1b"],
        "private_subnets": ["a", "b"],
        "public_subnets": ["c", "d"],
    }
    result = check({"network": {"vpc": vpc}, "n8n": {"license": BUSINESS}})
    assert "L9" in codes(result, "info")


def test_l10_env_sso_needs_recent_version() -> None:
    sso = {"kind": "saml", "metadata_url": "https://idp/metadata"}
    result = check(
        {"auth": {"n8n_sso": sso}, "n8n": {"version": "2.10.0", "license": BUSINESS}}
    )
    assert "L10" in codes(result)


# ------------------------------------------------------------------ finops


def test_f1_office_hours_not_in_production() -> None:
    result = check({"finops": {"schedule": {"kind": "office-hours"}}})
    assert "F1" in codes(result)
    dev = check(
        {"environment": "dev", "finops": {"schedule": {"kind": "office-hours"}}}
    )
    assert dev.ok


def test_f2_ceiling_is_recorded() -> None:
    result = check({"finops": {"max_monthly_usd": 100}})
    assert "F2" in codes(result, "info")


def test_f3_cost_cliffs_outside_production() -> None:
    result = check(
        {
            "environment": "staging",
            "profile": "xlarge",
            "n8n": {"license": ENTERPRISE},
        }
    )
    assert result.ok, result.findings
    warnings = {f.path for f in result.findings if f.code == "F3"}
    assert {"network.vpc.nat", "database.kind", "queue.kind"} <= warnings


def test_f3_aurora_never_pauses_while_n8n_runs() -> None:
    db = {"kind": "aurora-serverless", "min_acu": 0, "max_acu": 2}
    always_on = check({"database": db})
    paths = {f.path for f in always_on.findings if f.code == "F3"}
    assert "database.min_acu" in paths
    scheduled = check(
        {
            "database": db,
            "environment": "dev",
            "finops": {"schedule": {"kind": "office-hours"}},
        }
    )
    paths = {f.path for f in scheduled.findings if f.code == "F3"}
    assert "database.min_acu" not in paths


def test_f4_budget_without_emails() -> None:
    result = check({"finops": {"budget": {"monthly_usd": 50}}})
    assert "F4" in codes(result, "warning")


def test_f5_dashboard_cost_and_production_without_dashboard() -> None:
    result = check()
    assert "F5" in codes(result, "info")
    off = check({"observability": {"dashboard": {"enabled": False}}})
    assert "F5" in codes(off, "warning")
    dev = check(
        {"environment": "dev", "observability": {"dashboard": {"enabled": False}}}
    )
    assert "F5" not in codes(dev, "warning")


def test_dashboard_period_and_range_are_checked() -> None:
    bad = check({"observability": {"dashboard": {"period_minutes": 2}}})
    assert not bad.ok
    bad = check({"observability": {"dashboard": {"range": "3 hours"}}})
    assert not bad.ok
    assert check({"observability": {"dashboard": {"range": "2w"}}}).ok


def test_t16_cpu_scaling_cannot_start_from_zero_workers() -> None:
    queued = {"mode": "queue", "workers": {"min": 0, "scale_on": "cpu", "target": 70}}
    result = check({"topology": queued, "queue": {"kind": "elasticache-serverless"}})
    assert "T16" in codes(result, "error")
    queued["workers"] = {"min": 0, "scale_on": "queue-depth"}
    result = check({"topology": queued, "queue": {"kind": "elasticache-serverless"}})
    assert result.ok, result.findings


def test_t16_cpu_target_is_a_percentage() -> None:
    queued = {"mode": "queue", "workers": {"scale_on": "cpu", "target": 5}}
    result = check({"topology": queued, "queue": {"kind": "elasticache-serverless"}})
    assert "T16" in codes(result, "error")


def test_f6_metrics_cost() -> None:
    queued = {"mode": "queue"}
    result = check({"topology": queued, "queue": {"kind": "elasticache-serverless"}})
    assert "F6" in codes(result, "info")
    assert "F6" not in codes(check(), "info")


def test_t7_edge_login_wall_needs_a_domain_and_few_paths() -> None:
    result = check({"auth": {"mode": "cloudfront-cognito"}})
    assert "ingress.domain" in {f.path for f in result.findings if f.code == "T7"}
    many = [f"/extra-{i}/*" for i in range(11)]
    result = check({"auth": {"mode": "cloudfront-cognito", "public_paths": many}})
    assert "auth.public_paths" in {f.path for f in result.findings if f.code == "T7"}


def test_f7_edge_login_wall_cost() -> None:
    assert "F7" in codes(check({"auth": {"mode": "cloudfront-cognito"}}), "info")
    assert "F7" not in codes(check(), "info")
