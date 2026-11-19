from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from n8n_on_aws.cli import main
from n8n_on_aws.config import set_override
from n8n_on_aws.config.profiles import PROFILES, apply_profile, deep_merge
from n8n_on_aws.plan import build_plan

from .conftest import BASE, FIXTURES, check, merge

ENTERPRISE = {
    "edition": "enterprise",
    "activation_key": {"secret": "n8n/license"},
}

# ---------------------------------------------------------------- profiles


def test_every_profile_resolves() -> None:
    for name in PROFILES:
        extra: dict[str, Any] = {"profile": name}
        if name == "xlarge":
            extra["n8n"] = {"license": ENTERPRISE}
        result = check(extra)
        assert result.ok, (name, result.findings)


def test_explicit_key_beats_profile() -> None:
    merged = apply_profile(merge(BASE, {"topology": {"main": {"cpu": 2048}}}))
    assert merged["topology"]["main"]["cpu"] == 2048
    assert merged["topology"]["main"]["memory"] == 1024  # from the small profile


def test_other_kind_replaces_profile_block() -> None:
    merged = deep_merge(
        {"database": {"kind": "aurora-serverless", "min_acu": 1}},
        {"database": {"kind": "rds"}},
    )
    assert merged["database"] == {"kind": "rds"}


def test_queue_topology_on_nano_gets_postgres() -> None:
    result = check({"profile": "nano", "topology": {"mode": "queue"}})
    assert result.deployment is not None
    assert result.deployment.database.kind == "aurora-serverless"
    assert result.deployment.queue.kind == "elasticache-serverless"


def test_dev_disables_deletion_protection() -> None:
    result = check({"environment": "dev"})
    assert result.deployment is not None
    assert result.deployment.database.kind == "aurora-serverless"
    assert result.deployment.database.deletion_protection is False


def test_set_override() -> None:
    data: dict[str, Any] = {}
    set_override(data, "topology.workers.max=12")
    set_override(data, "ingress.kind=alb")
    assert data == {"topology": {"workers": {"max": 12}}, "ingress": {"kind": "alb"}}


# -------------------------------------------------------------------- plan


def test_plan_split_routes_webhooks() -> None:
    result = check({"profile": "large"})
    assert result.deployment is not None
    plan = build_plan(result.deployment)
    names = [role.name for role in plan.roles]
    assert names == ["main", "webhook", "worker"]
    main_env = plan.roles[0].env
    assert main_env["N8N_DISABLE_PRODUCTION_MAIN_PROCESS"] == "true"
    assert main_env["EXECUTIONS_MODE"] == "queue"
    assert plan.roles[2].env["QUEUE_HEALTH_CHECK_ACTIVE"] == "true"
    assert plan.roles[2].command == "n8n worker --concurrency=10"


def test_plan_secrets_never_inline() -> None:
    result = check(
        {
            "n8n": {"license": ENTERPRISE},
            "topology": {"mode": "multi-main"},
            "storage": {"bucket": {"kind": "create"}, "binary_data": "s3"},
        }
    )
    assert result.ok, result.findings
    assert result.deployment is not None
    plan = build_plan(result.deployment)
    main = plan.roles[0]
    assert "N8N_LICENSE_ACTIVATION_KEY" in main.secrets
    assert "N8N_LICENSE_ACTIVATION_KEY" not in main.env
    assert main.env["N8N_MULTI_MAIN_SETUP_ENABLED"] == "true"
    assert main.env["N8N_EXTERNAL_STORAGE_S3_AUTH_AUTO_DETECT"] == "true"
    assert plan.tags["n8n-on-aws:edition"] == "enterprise"


def test_plan_sqlite_on_efs() -> None:
    result = check({"profile": "nano", "environment": "dev"})
    assert result.deployment is not None
    plan = build_plan(result.deployment)
    assert plan.roles[0].env["DB_TYPE"] == "sqlite"
    assert plan.roles[0].env["N8N_DEFAULT_BINARY_DATA_MODE"] == "filesystem"
    assert plan.data_services["efs"] == "create"


def test_plan_user_env_override_is_reported() -> None:
    result = check({"n8n": {"env": {"N8N_LOG_LEVEL": "debug", "FOO": "bar"}}})
    assert result.deployment is not None
    plan = build_plan(result.deployment)
    assert plan.roles[0].env["N8N_LOG_LEVEL"] == "debug"
    assert [f.code for f in plan.findings] == ["P1"]


def test_plan_public_paths_only_with_wall() -> None:
    open_plan = check()
    assert open_plan.deployment is not None
    assert build_plan(open_plan.deployment).public_paths == []
    walled = check({"auth": {"mode": "cloudfront-cognito"}})
    assert walled.deployment is not None
    assert "/webhook/*" in build_plan(walled.deployment).public_paths


def test_public_paths_cover_n8n_routes_without_session() -> None:
    # Routes n8n 2.48 serves to callers that carry no n8n session: webhooks,
    # forms, MCP (trigger and instance server with its OAuth endpoints), health,
    # signed binary-data links and the OAuth client metadata document.
    walled = check({"auth": {"mode": "cloudfront-cognito"}})
    assert walled.deployment is not None
    assert build_plan(walled.deployment).public_paths == [
        "/webhook/*",
        "/webhook-waiting/*",
        "/form/*",
        "/form-waiting/*",
        "/mcp/*",
        "/healthz*",
        "/mcp-server/*",
        "/mcp-oauth/*",
        "/.well-known/oauth-*",
        "/rest/binary-data/signed",
        "/rest/oauth2-credential/client-metadata.json",
    ]


def test_test_paths_and_oauth_callback_stay_behind_the_wall() -> None:
    walled = check({"auth": {"mode": "cloudfront-cognito"}})
    assert walled.deployment is not None
    public = build_plan(walled.deployment).public_paths
    for path in ("/webhook-test/*", "/form-test/*", "/mcp-test/*", "/rest/*"):
        assert path not in public


def test_binary_data_mode_is_explicit_without_deprecated_modes() -> None:
    # n8n 2.48 defaults to `filesystem` outside queue mode, which is lost with a
    # Fargate task, so the mode is always set; the modes list is deprecated.
    for extra in (
        {},
        {"storage": {"binary_data": "s3"}, "n8n": {"license": ENTERPRISE}},
    ):
        result = check(extra)
        assert result.deployment is not None
        env = build_plan(result.deployment).roles[0].env
        assert "N8N_DEFAULT_BINARY_DATA_MODE" in env
        assert "N8N_AVAILABLE_BINARY_DATA_MODES" not in env


# --------------------------------------------------------------------- cli


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out)
    return code, out.getvalue()


def test_cli_dry_run_minimal(tmp_path: Path) -> None:
    path = str(FIXTURES / "minimal.yaml")
    code, text = run("deploy", "-f", path, "--dry-run", "-o", str(tmp_path))
    assert code == 0
    assert "nothing was deployed" in text
    assert "n8n start" in text


def test_cli_validate_reports_errors(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("apiVersion: n8n-on-aws/v1\nname: bad\n", encoding="utf-8")
    code, text = run("validate", "-f", str(path))
    assert code == 1
    assert "L1" in text


def test_cli_set_overrides(tmp_path: Path) -> None:
    code, text = run(
        "validate",
        "-f",
        str(FIXTURES / "minimal.yaml"),
        "--set",
        "topology.mode=multi-main",
    )
    assert code == 1
    assert "multi-main setup needs the enterprise edition" in text


def test_cli_plan_json() -> None:
    code, text = run(
        "plan", "-f", str(FIXTURES / "queue-workers.yaml"), "--format", "json"
    )
    assert code == 0
    payload = json.loads(text)
    assert payload["valid"] is True
    assert [role["name"] for role in payload["plan"]["roles"]] == ["main", "worker"]


def test_cli_missing_file() -> None:
    code, _ = run("validate", "-f", "does-not-exist.yaml")
    assert code == 2


def test_cli_schema_matches_committed(tmp_path: Path) -> None:
    target = tmp_path / "schema.json"
    code, _ = run("schema", "-o", str(target))
    assert code == 0
    committed = FIXTURES.parent.parent / "schema" / "n8n-on-aws.v1.json"
    assert json.loads(target.read_text()) == json.loads(committed.read_text())


def test_plan_webhook_url_variable_follows_version() -> None:
    recent = check({"n8n": {"version": "2.48.5"}})
    assert recent.deployment is not None
    env = build_plan(recent.deployment).roles[0].env
    assert "N8N_WEBHOOK_URL" in env and "WEBHOOK_URL" not in env
    older = check({"n8n": {"version": "2.20.0"}})
    assert older.deployment is not None
    env = build_plan(older.deployment).roles[0].env
    assert "WEBHOOK_URL" in env and "N8N_WEBHOOK_URL" not in env


def test_serverless_queue_uses_a_plain_tls_client() -> None:
    # n8n's cluster client cannot reach ElastiCache Serverless over TLS.
    result = check({"topology": {"mode": "queue"}})
    assert result.deployment is not None
    plan = build_plan(result.deployment)
    env = next(role for role in plan.roles if role.name == "worker").env
    assert env["QUEUE_BULL_REDIS_HOST"] == "${queue.endpoint}"
    assert env["QUEUE_BULL_REDIS_TLS"] == "true"
    assert env["QUEUE_BULL_PREFIX"] == "{n8n}"
    assert "QUEUE_BULL_REDIS_CLUSTER_NODES" not in env
