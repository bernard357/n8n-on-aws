from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from n8n_on_aws.config import LoadResult, resolve

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "configuration"

BASE: dict[str, Any] = {
    "apiVersion": "n8n-on-aws/v1",
    "name": "n8n-test",
    "n8n": {
        "version": "2.37.1",
        "license": {
            "usage": {
                "purpose": "internal-business",
                "accept_sustainable_use_license": True,
            }
        },
    },
}


def merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def check(extra: dict[str, Any] | None = None) -> LoadResult:
    return resolve(merge(BASE, extra or {}))


def codes(result: LoadResult, level: str = "error") -> set[str]:
    return {f.code for f in result.findings if f.level == level}


@pytest.fixture
def base() -> dict[str, Any]:
    return copy.deepcopy(BASE)
