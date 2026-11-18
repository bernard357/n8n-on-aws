"""Load, resolve and validate an ``n8n-on-aws/v1`` configuration file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .models import Deployment
from .profiles import apply_profile
from .rules import Finding, evaluate

__all__ = ["Deployment", "Finding", "LoadResult", "load", "resolve", "set_override"]


class LoadResult(BaseModel):
    """A resolved deployment (when the file is well-formed) and every finding."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    deployment: Deployment | None
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        """True when there is no error-level finding."""
        return not any(f.level == "error" for f in self.findings)


def set_override(data: dict[str, Any], assignment: str) -> None:
    """Apply a ``--set dotted.path=value`` override; the value is parsed as YAML."""
    path, sep, raw = assignment.partition("=")
    if not sep or not path:
        raise ValueError(f"expected key=value, got '{assignment}'")
    keys = path.strip().split(".")
    node = data
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = yaml.safe_load(raw) if raw else ""


# Fields holding a kind-discriminated union: pydantic inserts the tag after them.
_UNION_FIELDS = {
    "kms_key",
    "encryption_key",
    "cluster",
    "vpc",
    "hosted_zone",
    "certificate",
    "waf",
    "federation",
    "cognito",
    "n8n_sso",
    "database",
    "queue",
    "bucket",
}


def _location(loc: tuple[int | str, ...]) -> str:
    parts: list[str] = []
    previous: int | str | None = None
    for part in loc:
        if not (previous in _UNION_FIELDS and isinstance(part, str)):
            parts.append(str(part))
        previous = part
    return ".".join(parts) or "(root)"


def resolve(user: dict[str, Any]) -> LoadResult:
    """Merge profile defaults under ``user``, build the model and run the rules."""
    merged = apply_profile(user)
    try:
        deployment = Deployment.model_validate(merged)
    except ValidationError as exc:
        findings = [
            Finding(
                level="error",
                code="S1",
                path=_location(error["loc"]),
                message=error["msg"],
            )
            for error in exc.errors()
        ]
        return LoadResult(deployment=None, findings=findings)
    return LoadResult(deployment=deployment, findings=evaluate(deployment))


def load(path: str | Path, overrides: list[str] | None = None) -> LoadResult:
    """Read a YAML file, apply ``--set`` overrides and resolve it."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        finding = Finding(level="error", code="S0", path="(file)", message=str(exc))
        return LoadResult(deployment=None, findings=[finding])
    if not isinstance(data, dict):
        finding = Finding(
            level="error", code="S0", path="(file)", message="expected a mapping"
        )
        return LoadResult(deployment=None, findings=[finding])
    for assignment in overrides or []:
        set_override(data, assignment)
    return resolve(data)
