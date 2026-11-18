"""n8n editions, feature gates and usage boundaries, as published by n8n.

Sources (read 2026-11-18): the Sustainable Use License (LICENSE.md in n8n-io/n8n),
the pricing page, the community-edition features page and the feature docs. See
``docs/research/06-licensing.md``. This module is the single place to correct when
n8n changes its terms.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

EDITION_RANK: dict[str, int] = {
    "community": 0,
    "registered-community": 1,
    "business": 2,
    "enterprise": 3,
}

COMMUNITY_EDITIONS = frozenset({"community", "registered-community"})
PAID_EDITIONS = frozenset({"business", "enterprise"})

# Purposes allowed by the Sustainable Use License: "only for your own internal
# business purposes or for non-commercial or personal use".
COMMUNITY_PURPOSES = frozenset({"internal-business", "personal", "non-commercial"})

# Business plan guidance on the pricing page: "For companies with < 100 employees".
BUSINESS_MAX_EMPLOYEES = 100

LICENSE_URL = "https://github.com/n8n-io/n8n/blob/master/LICENSE.md"
PRICING_URL = "https://n8n.io/pricing/"


class Feature(BaseModel):
    """A self-hosted feature that needs a minimum edition."""

    model_config = ConfigDict(frozen=True)

    name: str
    edition: str
    env_prefixes: tuple[str, ...] = ()


FEATURES: dict[str, Feature] = {
    "multi-main": Feature(
        name="multi-main setup",
        edition="enterprise",
        env_prefixes=("N8N_MULTI_MAIN_SETUP",),
    ),
    "s3-binary-data": Feature(
        name="external storage (S3) for binary data",
        edition="business",
        env_prefixes=("N8N_EXTERNAL_STORAGE_",),
    ),
    "s3-execution-data": Feature(
        name="external storage (S3) for execution data",
        edition="business",
    ),
    "sso": Feature(
        name="single sign-on (OIDC, SAML, LDAP)",
        edition="business",
        env_prefixes=("N8N_SSO_",),
    ),
    "log-streaming": Feature(
        name="log streaming",
        edition="enterprise",
        env_prefixes=("N8N_LOG_STREAMING",),
    ),
    "external-secrets": Feature(
        name="external secrets store",
        edition="enterprise",
        env_prefixes=("N8N_EXTERNAL_SECRETS",),
    ),
}

# Raw environment variables whose *value* turns on a gated feature.
ENV_VALUE_GATES: dict[tuple[str, str], str] = {
    ("N8N_DEFAULT_BINARY_DATA_MODE", "s3"): "s3-binary-data",
    ("N8N_EXECUTION_DATA_STORAGE_MODE", "s3"): "s3-execution-data",
}

# Raw environment variables that must go through ``n8n.license`` instead.
RESERVED_LICENSE_ENV = frozenset({"N8N_LICENSE_ACTIVATION_KEY", "N8N_LICENSE_CERT"})

# Minimum n8n versions for features that the generated configuration relies on.
MIN_VERSION_SUPPORTED = (2, 0, 0)
MIN_VERSION_ENV_SSO = (2, 18, 0)


def allows(edition: str, feature: str) -> bool:
    """True when ``edition`` includes ``feature``."""
    return EDITION_RANK[edition] >= EDITION_RANK[FEATURES[feature].edition]


def features_from_env(env: dict[str, str]) -> list[tuple[str, str]]:
    """Return ``(variable, feature)`` pairs for gated features turned on in ``env``."""
    found: list[tuple[str, str]] = []
    for variable, value in env.items():
        gate = ENV_VALUE_GATES.get((variable, value.strip().lower()))
        if gate:
            found.append((variable, gate))
            continue
        for key, feature in FEATURES.items():
            if any(variable.startswith(prefix) for prefix in feature.env_prefixes):
                found.append((variable, key))
                break
    return found


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Parse ``"2.37.1"`` into a tuple; return None for channels like ``stable``."""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])
