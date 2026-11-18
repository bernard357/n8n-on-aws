"""T-shirt profiles: default values merged *under* the user's file.

A profile never overrides an explicit key. When the user picks another ``kind`` for a
resource, the profile's block for that resource is dropped entirely, so keys of the
profile's kind cannot leak into the user's kind.
"""

from __future__ import annotations

import copy
from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "nano": {
        "topology": {"mode": "single", "main": {"cpu": 256, "memory": 512}},
        "runtime": {"ecs": {"capacity": "fargate-spot"}},
        "network": {"vpc": {"kind": "create", "nat": "none"}},
        "ingress": {"kind": "cloudfront", "cloudfront": {"origin": "task"}},
        "database": {"kind": "sqlite"},
    },
    "small": {
        "topology": {"mode": "single", "main": {"cpu": 512, "memory": 1024}},
        "network": {"vpc": {"kind": "create", "nat": "none"}},
        "ingress": {"kind": "cloudfront", "cloudfront": {"origin": "task"}},
        "database": {"kind": "aurora-serverless", "min_acu": 0.5, "max_acu": 2},
    },
    "medium": {
        "topology": {
            "mode": "queue",
            "main": {"cpu": 1024, "memory": 2048},
            "workers": {"min": 1, "max": 4, "cpu": 1024, "memory": 2048, "spot": True},
        },
        "network": {"vpc": {"kind": "create", "nat": "single"}},
        "ingress": {"kind": "alb"},
        "database": {"kind": "aurora-serverless", "min_acu": 0.5, "max_acu": 8},
        "queue": {"kind": "elasticache-serverless"},
    },
    "large": {
        "topology": {
            "mode": "split",
            "main": {"cpu": 1024, "memory": 2048},
            "webhook": {"min": 1, "max": 4, "cpu": 512, "memory": 1024},
            "workers": {"min": 2, "max": 10, "cpu": 1024, "memory": 2048, "spot": True},
        },
        "network": {"vpc": {"kind": "create", "nat": "per-az"}},
        "ingress": {"kind": "alb"},
        "database": {
            "kind": "aurora-serverless",
            "min_acu": 2,
            "max_acu": 16,
            "readers": 1,
        },
        "queue": {"kind": "elasticache-serverless"},
    },
    "xlarge": {
        "topology": {
            "mode": "multi-main",
            "main": {"replicas": 2, "cpu": 2048, "memory": 4096},
            "webhook": {"min": 2, "max": 10, "cpu": 1024, "memory": 2048},
            "workers": {"min": 4, "max": 40, "cpu": 2048, "memory": 4096},
        },
        "network": {"vpc": {"kind": "create", "nat": "per-az"}},
        "ingress": {"kind": "alb"},
        "database": {
            "kind": "aurora",
            "instance_class": "db.r7g.large",
            "instances": 2,
        },
        "queue": {"kind": "elasticache", "node_type": "cache.r7g.large", "replicas": 1},
    },
}

DEFAULT_PROFILE = "small"

# Resources whose block is replaced, not merged, when the user picks another kind.
_KIND_BLOCKS = {"database", "queue", "vpc", "cluster", "bucket", "certificate"}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``; ``override`` always wins."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            kinds_differ = (
                key in _KIND_BLOCKS
                and "kind" in value
                and value.get("kind") != current.get("kind")
            )
            merged[key] = (
                copy.deepcopy(value) if kinds_differ else deep_merge(current, value)
            )
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _get(data: dict[str, Any], *path: str) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def apply_profile(user: dict[str, Any]) -> dict[str, Any]:
    """Return the user's data with profile defaults merged underneath.

    Defaults adapt to explicit choices: a queue topology picked on a small profile
    gets PostgreSQL and a queue rather than the profile's SQLite, and so on. Explicit
    keys are never changed; contradictions are left for the rule engine to report.
    """
    name = user.get("profile", DEFAULT_PROFILE)
    profile = copy.deepcopy(PROFILES.get(name, PROFILES[DEFAULT_PROFILE]))

    mode = _get(user, "topology", "mode") or _get(profile, "topology", "mode")
    queued = mode != "single"

    if queued and _get(user, "database") is None:
        if _get(profile, "database", "kind") == "sqlite":
            profile["database"] = {"kind": "aurora-serverless"}
    if _get(user, "queue") is None:
        if not queued:
            profile["queue"] = {"kind": "none"}
        elif "queue" not in profile:
            profile["queue"] = {"kind": "elasticache-serverless"}
    if mode in ("split", "multi-main") and _get(user, "ingress", "kind") is None:
        if _get(profile, "ingress", "kind") == "cloudfront":
            profile["ingress"] = {"kind": "alb"}
    if mode == "multi-main" and _get(user, "topology", "main", "replicas") is None:
        profile.setdefault("topology", {}).setdefault("main", {})["replicas"] = 2
    if not queued and _get(user, "topology", "main", "replicas") is None:
        profile.setdefault("topology", {}).setdefault("main", {})["replicas"] = 1
    merged = deep_merge(profile, user)

    # Throwaway environments: no deletion protection unless the user asks for it.
    database = merged.get("database")
    if (
        merged.get("environment") == "dev"
        and isinstance(database, dict)
        and database.get("kind") in ("aurora-serverless", "aurora", "rds")
        and _get(user, "database", "deletion_protection") is None
    ):
        database["deletion_protection"] = False
    return merged
