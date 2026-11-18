from __future__ import annotations

import pytest

from n8n_on_aws.config import load
from n8n_on_aws.plan import build_plan

from .conftest import FIXTURES

SAMPLES = sorted(FIXTURES.glob("*.yaml"))


def test_samples_exist() -> None:
    names = {path.stem for path in SAMPLES}
    assert {"minimal", "nano", "enterprise-ha", "eks-existing-cluster"} <= names


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_is_valid(path) -> None:  # type: ignore[no-untyped-def]
    result = load(path)
    errors = [f for f in result.findings if f.level == "error"]
    assert result.ok, errors
    assert result.deployment is not None
    plan = build_plan(result.deployment)
    assert plan.roles[0].name == "main"


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_points_at_schema(path) -> None:  # type: ignore[no-untyped-def]
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first == "# yaml-language-server: $schema=../../schema/n8n-on-aws.v1.json"
