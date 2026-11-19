"""The monthly cost estimate (#8): pure functions, no AWS call."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from n8n_on_aws import cli
from n8n_on_aws.config import load
from n8n_on_aws.finops.estimate import (
    HOURS,
    REDIS_FACTOR,
    SPOT_FACTOR,
    estimate,
    price_table,
)
from n8n_on_aws.plan import build_plan

from .conftest import FIXTURES, check

# Pinned so that a price refresh or a model change shows up in review.
EXPECTED = {
    "minimal": (66.47, 68.97),
    "nano": (3.00, 3.60),
    "small-team": (88.19, 91.25),
    "queue-workers": (174.34, 186.70),
    "split-webhooks": (910.37, 993.47),
    "enterprise-ha": (1679.36, 2055.90),
    "existing-landing-zone": (88.85, 94.99),
}


def _estimate(extra: dict | None = None, path: Path | None = None):  # type: ignore[no-untyped-def]
    result = load(path) if path else check(extra or {})
    assert result.deployment is not None, result.findings
    return estimate(result.deployment, build_plan(result.deployment))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_samples_are_pinned(name: str) -> None:
    cost = _estimate(path=FIXTURES / f"{name}.yaml")
    fixed, total = EXPECTED[name]
    assert cost.fixed == pytest.approx(fixed, rel=0.02)
    assert cost.total == pytest.approx(total, rel=0.02)


def test_price_table_is_dated_and_covers_the_main_regions() -> None:
    table = price_table()
    assert table.date >= "2026-11-19"
    for region in ("us-east-1", "eu-west-1", "eu-west-3", "eu-central-1"):
        prices = table.regions[region]
        for key in ("fargate_arm_vcpu_hour", "aurora_acu_hour", "alb_hour"):
            assert prices[key] > 0, (region, key)


def test_fargate_line_follows_the_price_table() -> None:
    cost = _estimate({"aws": {"region": "eu-west-3"}})
    prices = price_table().regions["eu-west-3"]
    main = next(i for i in cost.items if i.item.startswith("Fargate main"))
    hourly = 0.5 * prices["fargate_arm_vcpu_hour"] + 1 * prices["fargate_arm_gb_hour"]
    assert main.fixed == pytest.approx(hourly * HOURS)


def test_spot_workers_keep_one_on_demand_task() -> None:
    queued = {
        "topology": {
            "mode": "queue",
            "workers": {"min": 3, "max": 3, "spot": True, "cpu": 1024, "memory": 2048},
        },
        "queue": {"kind": "elasticache-serverless"},
    }
    spot = next(i for i in _estimate(queued).items if "worker" in i.item)
    queued["topology"]["workers"]["spot"] = False  # type: ignore[index]
    plain = next(i for i in _estimate(queued).items if "worker" in i.item)
    assert spot.fixed == pytest.approx(plain.fixed * (1 + 2 * SPOT_FACTOR) / 3)


def test_office_hours_scale_compute_and_paused_aurora() -> None:
    always = _estimate({"database": {"kind": "aurora-serverless", "min_acu": 0}})
    office = _estimate(
        {
            "environment": "dev",
            "database": {"kind": "aurora-serverless", "min_acu": 0},
            "finops": {"schedule": {"kind": "office-hours"}},
        }
    )
    ratio = office.hours / HOURS
    assert ratio == pytest.approx(5 * 11 * 52 / 12 / HOURS)
    for component in ("compute", "database"):
        assert office.by_component()[component][0] == pytest.approx(
            always.by_component()[component][0] * ratio, rel=0.01
        )


def test_aurora_at_zero_is_priced_at_half_an_acu_while_n8n_runs() -> None:
    zero = _estimate({"database": {"kind": "aurora-serverless", "min_acu": 0}})
    half = _estimate({"database": {"kind": "aurora-serverless", "min_acu": 0.5}})
    assert zero.by_component()["database"] == half.by_component()["database"]


def test_usage_assumptions_override_the_profile() -> None:
    base = _estimate()
    busy = _estimate({"finops": {"usage": {"logs_gb": 100, "average_acu": 2}}})
    assert busy.assumptions["logs_gb"] == 100
    assert busy.usage > base.usage
    assert busy.fixed == pytest.approx(base.fixed)


def test_redis_costs_more_than_valkey() -> None:
    queued = {"topology": {"mode": "queue"}}
    valkey = _estimate({**queued, "queue": {"kind": "elasticache-serverless"}})
    redis = _estimate(
        {**queued, "queue": {"kind": "elasticache-serverless", "engine": "redis"}}
    )
    v = valkey.by_component()["queue"][0]
    assert redis.by_component()["queue"][0] == pytest.approx(
        v * REDIS_FACTOR["serverless"]
    )


def test_unknown_region_and_class_are_reported_not_hidden() -> None:
    cost = _estimate(
        {
            "aws": {"region": "sa-east-1"},
            "database": {"kind": "rds", "instance_class": "db.x2g.16xlarge"},
        }
    )
    assert cost.priced_in == "us-east-1"
    assert any("sa-east-1" in n for n in cost.notes)
    assert any("db.x2g.16xlarge" in n for n in cost.notes)


def test_waf_and_edge_wall_lines() -> None:
    cost = _estimate(
        {
            "ingress": {"cloudfront": {"waf": {"kind": "managed"}}},
            "auth": {"mode": "cloudfront-cognito"},
        }
    )
    items = {i.item.split(":")[0]: i for i in cost.items}
    assert items["AWS WAF on CloudFront"].fixed == pytest.approx(5 + 3 * 1)
    assert items["Lambda@Edge login wall"].usage > 0


# ------------------------------------------------------------------ cli


def test_plan_prints_a_one_line_estimate_and_details_with_cost() -> None:
    path = str(FIXTURES / "minimal.yaml")
    out = io.StringIO()
    assert cli.main(["plan", "-f", path], out) == 0
    assert "Estimate: about 69 USD a month (66 fixed)" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["plan", "-f", path, "--cost"], out) == 0
    text = out.getvalue()
    assert "Monthly cost estimate (USD, us-east-1, prices of" in text
    assert "Aurora Serverless v2" in text and "assumptions:" in text


def test_plan_json_carries_the_cost() -> None:
    out = io.StringIO()
    path = str(FIXTURES / "queue-workers.yaml")
    assert cli.main(["plan", "-f", path, "--format", "json", "--cost"], out) == 0
    payload = json.loads(out.getvalue())
    assert payload["cost"]["total"] == pytest.approx(
        EXPECTED["queue-workers"][1], rel=0.02
    )
    assert {i["component"] for i in payload["cost"]["items"]} >= {"compute", "queue"}


def test_ceiling_fails_plan_and_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    path = str(FIXTURES / "minimal.yaml")
    out = io.StringIO()
    over = ["--set", "finops.max_monthly_usd=20"]
    assert cli.main(["plan", "-f", path, *over], out) == 1
    assert "above the ceiling of 20" in out.getvalue()
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/npx")
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    assert cli.main(["deploy", "-f", path, *over], io.StringIO()) == 1
    assert calls == []
    assert (
        cli.main(
            ["plan", "-f", path, "--set", "finops.max_monthly_usd=500"], io.StringIO()
        )
        == 0
    )
